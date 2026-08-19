from __future__ import annotations

import os
from datetime import datetime, time, timezone

import pandas as pd
import streamlit as st

from crm import CRMStore
from crm_ui import render_crm_section
from lead_finder import Lead
from outreach import OutreachConfig, OutreachStore, render_sequence, segment_for_lead
from outreach_integrations import ImapReplyClient, IntegrationError, TelegramBotClient, UnisenderProvider
from outreach_worker import OutreachWorker


UTC = timezone.utc


def _lead_label(lead: Lead) -> str:
    return f"{lead.name} — {lead.email or 'без email'}"


def _safe_table(rows: list[dict[str, object]], columns: list[str] | None = None) -> None:
    if not rows:
        st.caption("Пока нет данных.")
        return
    frame = pd.DataFrame(rows)
    if columns:
        frame = frame[[column for column in columns if column in frame.columns]]
    st.dataframe(frame, hide_index=True, width="stretch")


def render_outreach_section(db_path: str, leads: list[Lead], dry_results: bool = False) -> None:
    st.divider()
    st.header("Lead Outreach")
    st.caption("Email-цепочки только при доказанном согласии; Telegram — только после /start лида.")
    if dry_results:
        st.info("Поисковый Dry Run не записывает лидов. Для рассылок выберите сохранённые данные.")
        return

    mode = st.radio(
        "Режим",
        ["Рассылки", "Привлечение и CRM"],
        horizontal=True,
        key="lead_outreach_mode",
    )
    if mode == "Привлечение и CRM":
        st.caption(
            "Ручной нетворкинг, партнёры, сделки и финансы. CRM не отправляет сообщения и не меняет согласия."
        )
        render_crm_section(db_path, leads)
        return

    store = OutreachStore(db_path)
    config = OutreachConfig.from_env(dict(os.environ))
    permission_tab, draft_tab, campaign_tab, inbox_tab, delivery_tab = st.tabs(
        ["Контакты и согласия", "Черновики", "Кампании", "Входящие", "Доставляемость"]
    )

    with permission_tab:
        email_leads = [lead for lead in leads if lead.email]
        if not email_leads:
            st.caption("Нет сохранённых лидов с email.")
        else:
            lead_by_key = {lead.lead_key: lead for lead in email_leads}
            selected_key = st.selectbox(
                "Лид",
                list(lead_by_key),
                format_func=lambda key: _lead_label(lead_by_key[key]),
                key="outreach_permission_lead",
            )
            selected = lead_by_key[selected_key]
            permission = store.get_permission(selected.lead_key, "email", selected.email)
            status_options = ["unknown", "consented", "withdrawn"]
            selected_status = st.selectbox(
                "Разрешение email",
                status_options,
                index=status_options.index(str(permission["status"]))
                if permission["status"] in status_options
                else 0,
                format_func={
                    "unknown": "Неизвестно — отправка запрещена",
                    "consented": "Согласие подтверждено",
                    "withdrawn": "Отозвано — глобальная блокировка",
                }.get,
                key="outreach_permission_status",
            )
            source = st.text_input(
                "Источник согласия",
                value=str(permission["source"] or ""),
                key="outreach_permission_source",
            )
            evidence = st.text_area(
                "Доказательство",
                value=str(permission["evidence"] or ""),
                help="Например: ID записи формы double opt-in или ссылка на договор.",
                key="outreach_permission_evidence",
            )
            obtained_date = st.date_input("Дата получения", key="outreach_permission_date")
            if st.button("Сохранить разрешение", key="outreach_permission_save"):
                obtained_at = datetime.combine(obtained_date, time.min, tzinfo=UTC).isoformat()
                try:
                    store.upsert_permission(
                        selected.lead_key,
                        "email",
                        selected.email,
                        selected_status,
                        source=source,
                        evidence=evidence,
                        obtained_at=obtained_at if selected_status == "consented" else None,
                    )
                    st.success("Разрешение сохранено.")
                except ValueError as error:
                    st.error(str(error))
        _safe_table(
            store.list_permissions(),
            ["lead_name", "channel", "address", "status", "source", "evidence", "obtained_at", "revoked_at"],
        )

    with draft_tab:
        candidates = [lead for lead in leads if segment_for_lead(lead)]
        if not candidates:
            st.caption("Нет лидов с подтверждённым основанием для сообщения.")
        else:
            lead_by_key = {lead.lead_key: lead for lead in candidates}
            draft_key = st.selectbox(
                "Лид для предпросмотра",
                list(lead_by_key),
                format_func=lambda key: lead_by_key[key].name,
                key="outreach_draft_lead",
            )
            draft_lead = lead_by_key[draft_key]
            try:
                sequence = render_sequence(draft_lead)
                preview = pd.DataFrame(
                    [{"Шаг": item.step_index + 1, "Тема": item.subject, "Текст": item.body} for item in sequence]
                )
                st.dataframe(preview, hide_index=True, width="stretch")
                if st.button("Сохранить первый черновик", key="outreach_save_draft"):
                    store.create_draft(draft_lead)
                    st.success("Черновик сохранён без отправки.")
            except ValueError as error:
                st.error(str(error))
        _safe_table(store.list_drafts(), ["lead_name", "subject", "body", "updated_at"])

    with campaign_tab:
        with st.expander("Новая кампания"):
            campaign_name = st.text_input("Название", key="outreach_campaign_name")
            segment = st.selectbox(
                "Сегмент",
                ["no_site", "existing_site"],
                format_func=lambda value: "Нет сайта / сайт сломан" if value == "no_site" else "Есть сайт",
                key="outreach_campaign_segment",
            )
            timezone_name = st.text_input(
                "Часовой пояс",
                value="Asia/Yekaterinburg",
                key="outreach_campaign_timezone",
            )
            daily_limit = st.number_input(
                "Стартовый лимит в день", min_value=1, max_value=5, value=5, key="outreach_campaign_limit"
            )
            if st.button("Создать черновик кампании", key="outreach_campaign_create"):
                try:
                    store.create_campaign(campaign_name, segment, timezone_name, int(daily_limit))
                    st.success("Кампания создана в статусе draft.")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))

        campaigns = store.list_campaigns()
        _safe_table(campaigns, ["id", "name", "segment", "timezone", "daily_limit", "state", "recipients"])
        if campaigns:
            by_id = {int(row["id"]): row for row in campaigns}
            campaign_id = st.selectbox(
                "Кампания",
                list(by_id),
                format_func=lambda value: f"#{value} {by_id[value]['name']} ({by_id[value]['state']})",
                key="outreach_campaign_selected",
            )
            campaign = by_id[campaign_id]
            state_columns = st.columns(3)
            if state_columns[0].button("Утвердить", key="outreach_campaign_approve"):
                try:
                    store.set_campaign_state(campaign_id, "approved")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
            if state_columns[1].button("Запустить", key="outreach_campaign_activate"):
                try:
                    store.set_campaign_state(campaign_id, "active")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
            if state_columns[2].button("Пауза", key="outreach_campaign_pause"):
                try:
                    store.set_campaign_state(campaign_id, "paused")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))

            eligible = [
                lead
                for lead in leads
                if lead.email
                and segment_for_lead(lead) == campaign["segment"]
                and store.can_contact(lead.lead_key, "email", lead.email)
            ]
            eligible_by_key = {lead.lead_key: lead for lead in eligible}
            selected_leads = st.multiselect(
                "Получатели с разрешением",
                list(eligible_by_key),
                format_func=lambda key: _lead_label(eligible_by_key[key]),
                key="outreach_campaign_recipients",
            )
            if st.button("Добавить получателей", key="outreach_campaign_enroll"):
                enrolled = 0
                for lead_key in selected_leads:
                    try:
                        store.enroll_recipient(campaign_id, eligible_by_key[lead_key])
                        enrolled += 1
                    except (PermissionError, ValueError) as error:
                        st.error(f"{eligible_by_key[lead_key].name}: {error}")
                st.success(f"Добавлено получателей: {enrolled}.")

            worker = OutreachWorker(store, config)
            run_columns = st.columns(2)
            if run_columns[0].button("Dry-run очереди", key="outreach_campaign_dry_run"):
                result = worker.run_once(dry_run=True)
                if result.previews:
                    st.dataframe(pd.DataFrame(result.previews), hide_index=True, width="stretch")
                else:
                    st.info("Сейчас нет сообщений, готовых к отправке.")
            ready, missing = store.production_gate_ready()
            if run_columns[1].button(
                "Отправить готовые сейчас",
                disabled=not (ready and config.email_ready),
                key="outreach_campaign_send",
            ):
                try:
                    result = worker.run_once()
                    st.success(f"Отправлено: {result.sent}; пропущено: {result.skipped}; ошибок: {result.failed}.")
                except (IntegrationError, PermissionError, ValueError) as error:
                    st.error(str(error))
            seed_candidates = [lead for lead in leads if segment_for_lead(lead) == campaign["segment"]]
            if st.button(
                "Отправить seed-тест на адрес отправителя",
                disabled=not (config.email_ready and seed_candidates),
                key="outreach_campaign_seed_test",
            ):
                try:
                    sample = render_sequence(seed_candidates[0], str(campaign["segment"]))[0]
                    UnisenderProvider(config).send_test_message(sample.subject, sample.body)
                    st.success(f"Тест отправлен на {config.sender_email}. Получателей кампании он не затронул.")
                except (IntegrationError, ValueError) as error:
                    st.error(str(error))
            if not ready:
                st.caption("Отправка заблокирована: " + "; ".join(missing))
            if int(campaign["daily_limit"]) >= 4:
                max_next_limit = int(int(campaign["daily_limit"]) * 1.25)
                requested_limit = st.number_input(
                    "Новый дневной лимит",
                    min_value=int(campaign["daily_limit"]) + 1,
                    max_value=max_next_limit,
                    value=max_next_limit,
                    key="outreach_campaign_new_limit",
                )
                if st.button("Повысить лимит после 7 дней", key="outreach_campaign_increase_limit"):
                    try:
                        store.increase_daily_limit(campaign_id, int(requested_limit))
                        st.success("Лимит увеличен не более чем на 25%.")
                        st.rerun()
                    except ValueError as error:
                        st.error(str(error))

    with inbox_tab:
        sync_columns = st.columns(2)
        if sync_columns[0].button(
            "Получить email-ответы",
            disabled=not config.imap_ready,
            key="outreach_sync_imap",
        ):
            try:
                count = ImapReplyClient(config).sync(store)
                st.success(f"Новых ответов: {count}.")
            except IntegrationError as error:
                st.error(str(error))
        if sync_columns[1].button(
            "Получить Telegram-сообщения",
            disabled=not config.telegram_ready,
            key="outreach_sync_telegram",
        ):
            try:
                count = TelegramBotClient(config).sync(store)
                st.success(f"Обработано Telegram-сообщений: {count}.")
            except IntegrationError as error:
                st.error(str(error))

        st.subheader("Email-события")
        events = store.list_events()
        _safe_table(events, ["id", "occurred_at", "lead_key", "event_type", "channel"])
        reply_events = [event for event in events if event["event_type"] == "reply" and event["lead_key"]]
        if reply_events:
            reply_by_id = {int(event["id"]): event for event in reply_events}
            event_id = st.selectbox(
                "Ответ для классификации",
                list(reply_by_id),
                format_func=lambda value: f"#{value} — {reply_by_id[value]['lead_key']}",
                key="outreach_inbox_event",
            )
            classification = st.selectbox(
                "Классификация",
                ["positive", "question", "neutral", "negative"],
                format_func={
                    "positive": "Положительный ответ",
                    "question": "Вопрос",
                    "neutral": "Нейтральный",
                    "negative": "Отказ",
                }.get,
                key="outreach_inbox_classification",
            )
            next_step = st.text_input(
                "Следующий шаг",
                value="Подготовить короткое демо" if classification == "positive" else "",
                key="outreach_inbox_next_step",
            )
            if st.button("Сохранить классификацию", key="outreach_inbox_classify"):
                selected_event = reply_by_id[event_id]
                store.set_inbox_action(
                    "email_event",
                    event_id,
                    str(selected_event["lead_key"]),
                    classification,
                    next_step,
                )
                st.success("Классификация и следующий шаг сохранены.")
        st.subheader("Telegram")
        telegram_messages = store.list_telegram_messages()
        _safe_table(telegram_messages, ["created_at", "lead_name", "chat_id", "direction", "text"])
        inbound_telegram = [
            row for row in telegram_messages if row["direction"] == "inbound" and row["lead_key"]
        ]
        if inbound_telegram:
            telegram_by_id = {int(row["update_id"]): row for row in inbound_telegram}
            telegram_update_id = st.selectbox(
                "Telegram-ответ для классификации",
                list(telegram_by_id),
                format_func=lambda value: f"#{value} — {telegram_by_id[value]['lead_name']}",
                key="outreach_telegram_inbox_event",
            )
            telegram_classification = st.selectbox(
                "Классификация Telegram",
                ["positive", "question", "neutral", "negative"],
                format_func={
                    "positive": "Положительный ответ",
                    "question": "Вопрос",
                    "neutral": "Нейтральный",
                    "negative": "Отказ",
                }.get,
                key="outreach_telegram_inbox_classification",
            )
            telegram_next_step = st.text_input(
                "Следующий шаг по Telegram",
                value="Квалифицировать интерес" if telegram_classification == "positive" else "",
                key="outreach_telegram_inbox_next_step",
            )
            if st.button("Сохранить классификацию Telegram", key="outreach_telegram_inbox_classify"):
                selected_message = telegram_by_id[telegram_update_id]
                store.set_inbox_action(
                    "telegram_update",
                    telegram_update_id,
                    str(selected_message["lead_key"]),
                    telegram_classification,
                    telegram_next_step,
                )
                st.success("Классификация Telegram сохранена. Никаких сообщений не отправлено.")
        inbox_actions = store.list_inbox_actions()
        _safe_table(
            inbox_actions,
            ["updated_at", "source", "source_id", "lead_key", "classification", "next_step"],
        )
        positive_actions = [
            row for row in inbox_actions if row["classification"] == "positive" and row["lead_key"]
        ]
        if positive_actions:
            action_by_key = {
                f"{row['source']}:{row['source_id']}": row for row in positive_actions
            }
            action_key = st.selectbox(
                "Положительный ответ для CRM",
                list(action_by_key),
                format_func=lambda key: f"{action_by_key[key]['lead_key']} — {key}",
                key="outreach_positive_crm_action",
            )
            deal_title = st.text_input(
                "Предмет сделки",
                value="Разработка сайта",
                key="outreach_positive_crm_title",
            )
            if st.button("Создать сделку и задачу «квалифицировать»", key="outreach_positive_crm_create"):
                selected_action = action_by_key[action_key]
                try:
                    CRMStore(db_path).create_deal_from_inbox(
                        str(selected_action["source"]),
                        str(selected_action["source_id"]),
                        str(selected_action["lead_key"]),
                        deal_title,
                    )
                    st.success("Сделка и ручная задача созданы. Сообщения и Telegram-ссылки не отправлялись.")
                except ValueError as error:
                    st.error(str(error))
        active_chats = {
            str(row["address"]).removeprefix("tg:"): row
            for row in store.list_permissions()
            if row["channel"] == "telegram" and row["status"] == "inbound"
        }
        if active_chats:
            chat_id = st.selectbox("Диалог", list(active_chats), key="outreach_telegram_chat")
            answer = st.text_area("Ответ оператора", key="outreach_telegram_answer")
            if st.button("Отправить ответ в Telegram", key="outreach_telegram_reply"):
                try:
                    TelegramBotClient(config).reply(store, chat_id, answer.strip())
                    st.success("Ответ отправлен.")
                except (IntegrationError, PermissionError, ValueError) as error:
                    st.error(str(error))

        positive_lead_keys = {
            str(row["lead_key"])
            for row in inbox_actions
            if row["classification"] == "positive" and row["lead_key"]
        }
        link_candidates = [lead for lead in leads if lead.lead_key in positive_lead_keys]
        if link_candidates and config.telegram_ready:
            links_by_key = {lead.lead_key: lead for lead in link_candidates}
            link_lead_key = st.selectbox(
                "Персональная ссылка после положительного ответа",
                list(links_by_key),
                format_func=lambda key: links_by_key[key].name,
                key="outreach_telegram_link_lead",
            )
            if st.button("Создать одноразовую Telegram-ссылку", key="outreach_telegram_link"):
                link = store.create_telegram_link(
                    link_lead_key,
                    config.telegram_bot_username,
                    config.link_secret,
                )
                st.text_area(
                    "Готовый ответ",
                    value=(
                        "Спасибо за ответ. Подготовлю короткое демо без длинной презентации. "
                        f"Если удобнее продолжить в Telegram, вот персональная ссылка: {link}"
                    ),
                    key="outreach_generated_reply",
                )

    with delivery_tab:
        metrics = store.delivery_metrics()
        metric_columns = st.columns(4)
        metric_columns[0].metric("Отправлено", metrics.get("sent", 0))
        metric_columns[1].metric("Доставлено", metrics.get("delivered", 0))
        metric_columns[2].metric("Ответы", metrics.get("reply", 0))
        metric_columns[3].metric("Отказы и жалобы", metrics.get("unsubscribe", 0) + metrics.get("complaint", 0))
        st.subheader("Защитные проверки")
        gate_labels = {
            "dns_verified": "SPF, DKIM и DMARC подтверждены",
            "unsubscribe_verified": "Отписка проверена на контрольном адресе",
            "seed_delivery_verified": "Контрольная доставка прошла",
            "production_enabled": "Производственная отправка явно разрешена",
        }
        for key, label in gate_labels.items():
            checked = st.checkbox(label, value=store.get_setting(key, "0") == "1", key=f"outreach_gate_{key}")
            store.set_setting(key, checked)
        st.caption(
            "Секреты: "
            f"Unisender {'настроен' if config.email_ready else 'не настроен'}, "
            f"IMAP {'настроен' if config.imap_ready else 'не настроен'}, "
            f"Telegram {'настроен' if config.telegram_ready else 'не настроен'}. Значения не отображаются и не сохраняются в SQLite."
        )
        st.subheader("Глобальные запреты")
        _safe_table(store.list_suppressions(), ["channel", "address", "reason", "source", "created_at"])
        st.subheader("Сообщения")
        _safe_table(
            store.list_messages(),
            ["created_at", "lead_name", "channel", "step_index", "status", "subject", "provider_campaign_id"],
        )
