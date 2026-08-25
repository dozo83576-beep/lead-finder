from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from outreach import OutreachConfig, OutreachStore, WorkerResult, is_send_window, render_sequence
from outreach_integrations import (
    ImapReplyClient,
    IntegrationError,
    TelegramBotClient,
    UnisenderProvider,
    configure_outreach_logging,
    sync_unisender_messages,
)


UTC = timezone.utc
LOGGER = logging.getLogger("lead_finder.outreach")


class OutreachWorker:
    def __init__(
        self,
        store: OutreachStore,
        config: OutreachConfig,
        provider: Any | None = None,
        imap_client: Any | None = None,
        telegram_client: Any | None = None,
    ):
        self.store = store
        self.config = config
        self.provider = provider or UnisenderProvider(config)
        self.imap_client = imap_client or ImapReplyClient(config)
        self.telegram_client = telegram_client or TelegramBotClient(config)

    def sync_incoming(self) -> dict[str, int]:
        result = {"email_replies": 0, "telegram_updates": 0, "delivery_events": 0}
        if self.config.imap_ready:
            result["email_replies"] = int(self.imap_client.sync(self.store))
        if self.config.telegram_ready:
            result["telegram_updates"] = int(self.telegram_client.sync(self.store))
        if self.config.email_ready:
            result["delivery_events"] = int(sync_unisender_messages(self.store, self.provider))
        return result

    def run_once(
        self,
        *,
        dry_run: bool = False,
        now: datetime | None = None,
        sync: bool = True,
    ) -> WorkerResult:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if not dry_run:
            if not self.config.email_ready:
                raise IntegrationError("Email-отправка не настроена полностью")
            ready, missing = self.store.production_gate_ready()
            if not ready:
                raise PermissionError("Производственная отправка заблокирована: " + "; ".join(missing))
            if sync:
                self.sync_incoming()

        sent = 0
        skipped = 0
        failed = 0
        previews: list[dict[str, object]] = []
        campaign_counts: dict[int, int] = {}

        for recipient in self.store.due_recipients(current):
            campaign_id = int(recipient["campaign_id"])
            if not is_send_window(current, str(recipient["timezone"])):
                if not dry_run:
                    self.store.reschedule_recipient(
                        int(recipient["id"]), current, str(recipient["timezone"])
                    )
                skipped += 1
                continue
            if campaign_id not in campaign_counts:
                campaign_counts[campaign_id] = self.store.sent_today(
                    campaign_id,
                    current,
                    str(recipient["timezone"]),
                )
            if campaign_counts[campaign_id] >= int(recipient["daily_limit"]):
                skipped += 1
                continue

            lead = self.store.get_lead(str(recipient["lead_key"]))
            address = str(recipient["address"])
            if not lead or not self.store.can_contact(str(recipient["lead_key"]), "email", address):
                self.store.stop_by_destination("email", address, "withdrawn")
                skipped += 1
                continue

            next_step = int(recipient["current_step"]) + 1
            sequence = render_sequence(lead, str(recipient["segment"]))
            if next_step >= len(sequence):
                skipped += 1
                continue
            rendered = sequence[next_step]
            if dry_run:
                previews.append(
                    {
                        "campaign_id": campaign_id,
                        "lead_key": lead.lead_key,
                        "address": address,
                        "step": next_step,
                        "subject": rendered.subject,
                        "body": rendered.body,
                    }
                )
                campaign_counts[campaign_id] += 1
                continue

            claimed = self.store.claim_message_within_limit(
                recipient, rendered, current, int(recipient["daily_limit"])
            )
            if claimed is None:
                skipped += 1
                continue
            try:
                result = self.provider.send_message(
                    self.store,
                    lead.lead_key,
                    address,
                    rendered.subject,
                    rendered.body,
                    contact_name=lead.name,
                )
                self.store.mark_message_sent(int(claimed["id"]), result, current)
                campaign_counts[campaign_id] += 1
                sent += 1
            except Exception as error:
                self.store.mark_message_failed(int(claimed["id"]), str(error))
                LOGGER.error("Отправка сообщения %s завершилась ошибкой: %s", claimed["id"], type(error).__name__)
                failed += 1

        return WorkerResult(sent, skipped, failed, tuple(previews))


MAX_RETRY_DELAY = 1800


def retry_delay(interval: int, failures: int) -> int:
    """Пауза перед следующим циклом: растёт, пока ошибка повторяется.

    Отозванный токен или неверный пароль приложения не проходят сами собой. Без
    роста паузы worker молотил бы провайдера с постоянным интервалом сутками — прямая
    дорога к бану аккаунта. Успешный цикл сбрасывает счётчик обратно.
    """
    if failures <= 0:
        return interval
    # Потолок не должен опускать паузу ниже штатного интервала: при --interval больше
    # получаса это заставило бы worker после сбоя опрашивать провайдера чаще обычного.
    return max(interval, min(interval * 2 ** min(failures, 10), MAX_RETRY_DELAY))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Безопасный worker рассылок Lead Finder")
    parser.add_argument("--db-path", default=os.environ.get("LEAD_DB_PATH", "leads.db"))
    parser.add_argument("--dry-run", action="store_true", help="Не выполнять внешние вызовы и записи отправки")
    parser.add_argument("--loop", action="store_true", help="Повторять запуск с заданным интервалом")
    parser.add_argument("--interval", type=int, default=60)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.interval < 10:
        raise SystemExit("Интервал должен быть не меньше 10 секунд.")
    configure_outreach_logging()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = OutreachStore(args.db_path)
    config = OutreachConfig.from_env(dict(os.environ))
    worker = OutreachWorker(store, config)
    failures = 0
    while True:
        try:
            result = worker.run_once(dry_run=args.dry_run)
            LOGGER.info(
                "Цикл завершён: отправлено=%s пропущено=%s ошибок=%s предпросмотров=%s",
                result.sent,
                result.skipped,
                result.failed,
                len(result.previews),
            )
            failures = 0
        except sqlite3.OperationalError as error:
            # Например, база занята соседним процессом. В режиме --loop это повод
            # подождать следующий цикл, а не завершать долгоживущий worker.
            LOGGER.error("База недоступна в этом цикле: %s", error)
            if not args.loop:
                return 2
            failures += 1
        except IntegrationError as error:
            # Таймаут или 5xx провайдера — временная помеха. В режиме --loop worker
            # обязан пережить её: иначе останавливается синхронизация complaint
            # и hard bounce, на которой держится авто-пауза кампании.
            LOGGER.error("Ошибка интеграции в этом цикле: %s", error)
            if not args.loop:
                return 2
            failures += 1
        except (PermissionError, ValueError) as error:
            LOGGER.error("Worker остановлен: %s", error)
            return 2
        if not args.loop:
            return 0
        time.sleep(retry_delay(args.interval, failures))


if __name__ == "__main__":
    raise SystemExit(main())
