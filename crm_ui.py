from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import pandas as pd
import streamlit as st

from crm import (
    COMMUNITY_PLATFORMS,
    DEAL_STAGES,
    DEAL_TRANSITIONS,
    NETWORKING_ACTIONS,
    NOTE_TYPES,
    PARTNER_STATES,
    SOURCE_KINDS,
    SOURCE_STATES,
    CRMStore,
)
from lead_finder import Lead


UTC = timezone.utc
SOURCE_LABELS = {
    "search": "Поиск",
    "community": "Сообщество",
    "partner": "Партнёр",
    "inbound": "Входящий",
    "manual": "Ручной",
}
STAGE_LABELS = {
    "new": "Новая",
    "qualified": "Квалификация",
    "discovery": "Выявление задачи",
    "proposal": "Предложение",
    "negotiation": "Переговоры",
    "won": "Выиграна",
    "lost": "Проиграна",
}


def _table(rows: list[dict[str, object]], columns: list[str] | None = None) -> None:
    if not rows:
        st.caption("Пока нет данных.")
        return
    frame = pd.DataFrame(rows)
    if columns:
        frame = frame[[column for column in columns if column in frame.columns]]
    st.dataframe(frame, hide_index=True, width="stretch")


def _rubles(kopecks: object) -> str:
    value = int(kopecks or 0)
    return f"{Decimal(value) / Decimal(100):,.2f} ₽".replace(",", " ")


def _kopecks(value: str) -> int:
    try:
        rubles = Decimal((value or "").strip().replace(" ", "").replace(",", "."))
    except InvalidOperation as error:
        raise ValueError("Сумма должна быть числом в рублях.") from error
    if rubles <= 0:
        raise ValueError("Сумма должна быть больше нуля.")
    return int((rubles * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _date_start(value) -> str:
    return datetime.combine(value, time.min, tzinfo=UTC).isoformat()


def _lead_label(lead: Lead) -> str:
    return f"{lead.name} — {lead.city or 'город не указан'}"


def _show_error(action) -> None:
    try:
        action()
        st.success("Сохранено.")
    except (ValueError, PermissionError) as error:
        st.error(str(error))


def render_lead_crm_summary(db_path: str, lead: Lead) -> None:
    store = CRMStore(db_path)
    summary = store.lead_summary(lead.lead_key)
    with st.expander("Привлечение и CRM", expanded=False):
        profile = summary["profile"]
        active_deal = summary["active_deal"]
        if profile:
            source = profile.get("source_name") or SOURCE_LABELS.get(str(profile["source_kind"]), profile["source_kind"])
            st.write(f"**Источник:** {source}")
            if profile.get("partner_name"):
                st.write(f"**Партнёр:** {profile['partner_name']}")
            st.write(f"**Следующий шаг:** {profile.get('next_step') or 'не указан'}")
        else:
            st.caption("Лид ещё не добавлен в CRM. Это не влияет на его старый статус.")
            if st.button("Добавить лида в CRM", key=f"crm_add_lead_{lead.lead_key}"):
                store.add_lead_to_crm(
                    lead.lead_key,
                    "search" if lead.source else "manual",
                    next_step="Определить источник и следующий ручной шаг",
                )
                st.success("Лид добавлен в CRM без создания сделки и без изменения согласий.")
                st.rerun()
        if active_deal:
            st.write(
                f"**Активная сделка:** #{active_deal['id']} {active_deal['title']} — "
                f"{STAGE_LABELS.get(str(active_deal['stage']), active_deal['stage'])}"
            )
        permissions = summary["permissions"]
        if permissions:
            st.write("**Разрешения:** " + "; ".join(
                f"{item['channel']}: {item['status']}" for item in permissions
            ))
        activities = summary["activities"]
        if activities:
            st.write("**Последние касания:**")
            for item in activities:
                st.caption(f"{item['occurred_at']} · {item['action_type']} · {item['summary']}")


def _render_platforms(store: CRMStore, leads: list[Lead]) -> None:
    st.subheader("Площадки и ручной нетворкинг")
    st.caption(
        "Только учёт ручных наблюдений и полезных комментариев. Система ничего не публикует и не отправляет."
    )
    st.link_button("Открыть TGStat вручную", "https://tgstat.ru/")

    with st.expander("Добавить площадку", expanded=False):
        with st.form("crm_source_form"):
            kind = st.selectbox("Тип", SOURCE_KINDS, format_func=SOURCE_LABELS.get)
            platform = st.selectbox("Платформа сообщества", COMMUNITY_PLATFORMS, disabled=kind != "community")
            name = st.text_input("Название")
            url = st.text_input("URL")
            niche = st.text_input("Ниша")
            state = st.selectbox("Состояние", SOURCE_STATES)
            scores = st.columns(2)
            activity_score = scores[0].slider("Активность", 0, 5, 0)
            audience_fit_score = scores[1].slider("Соответствие аудитории", 0, 5, 0)
            notes = st.text_area("Короткая заметка")
            submitted = st.form_submit_button("Добавить площадку")
        if submitted:
            _show_error(lambda: store.create_source(
                kind, name, platform=platform if kind == "community" else "", url=url, niche=niche,
                state=state, activity_score=activity_score, audience_fit_score=audience_fit_score,
                notes=notes,
            ))
    sources = store.list_sources()
    _table(sources, ["id", "kind", "platform", "name", "niche", "state", "activity_score", "audience_fit_score", "url"])
    if sources:
        source_by_id = {int(row["id"]): row for row in sources}
        state_columns = st.columns(3)
        source_to_update = state_columns[0].selectbox(
            "Площадка для изменения", list(source_by_id),
            format_func=lambda key: source_by_id[key]["name"], key="crm_source_state_id"
        )
        source_state = state_columns[1].selectbox(
            "Новое состояние", SOURCE_STATES, key="crm_source_state_value"
        )
        if state_columns[2].button("Изменить состояние", key="crm_source_state_save"):
            _show_error(lambda: store.set_source_state(source_to_update, source_state))

    st.subheader("Профили позиционирования")
    with st.expander("Создать профиль", expanded=False):
        with st.form("crm_positioning_form"):
            platform_name = st.text_input("Платформа")
            audience = st.text_area("Целевая аудитория")
            value = st.text_area("Ценностное предложение")
            evidence = st.text_area("Подтверждённые доказательства")
            cta = st.text_input("CTA")
            submitted = st.form_submit_button("Сохранить черновик")
        if submitted:
            _show_error(lambda: store.create_positioning_profile(platform_name, audience, value, evidence, cta))
    profiles = store.list_positioning_profiles()
    if profiles:
        drafts = [row for row in profiles if row["state"] == "draft"]
        if drafts:
            profile_by_id = {int(row["id"]): row for row in drafts}
            selected_profile = st.selectbox(
                "Черновик для утверждения", list(profile_by_id),
                format_func=lambda key: f"#{key} — {profile_by_id[key]['platform']} / {profile_by_id[key]['audience']}"
            )
            if st.button("Утвердить профиль", key="crm_approve_profile"):
                _show_error(lambda: store.approve_positioning_profile(selected_profile))
    _table(profiles, ["id", "platform", "audience", "value_proposition", "evidence", "cta", "state"])

    st.subheader("Журнал нетворкинга")
    with st.expander("Записать ручное касание", expanded=False):
        source_options = {0: {"name": "Не выбрана"}, **{int(row["id"]): row for row in sources}}
        lead_options = {"": None, **{lead.lead_key: lead for lead in leads}}
        with st.form("crm_activity_form"):
            action_type = st.selectbox("Действие", NETWORKING_ACTIONS)
            source_id = st.selectbox(
                "Площадка", list(source_options), format_func=lambda key: source_options[key]["name"]
            )
            lead_key = st.selectbox(
                "Лид", list(lead_options),
                format_func=lambda key: "Не выбран" if not key else _lead_label(lead_options[key])
            )
            reference_url = st.text_input("Ссылка на ручное действие")
            summary = st.text_area("Короткое резюме")
            outcome = st.text_input("Результат")
            next_task = st.text_input("Следующая ручная задача")
            due_date = st.date_input("Срок задачи")
            submitted = st.form_submit_button("Записать касание")
        if submitted:
            _show_error(lambda: store.add_networking_activity(
                action_type, summary, acquisition_source_id=source_id or None, lead_key=lead_key or None,
                reference_url=reference_url, outcome=outcome, next_task=next_task,
                next_task_due_at=_date_start(due_date) if next_task else None,
            ))
    _table(store.list_networking_activities(), [
        "occurred_at", "action_type", "source_name", "lead_name", "partner_name", "summary", "outcome",
        "next_task", "next_task_due_at", "reference_url"
    ])


def _render_partners(store: CRMStore, leads: list[Lead]) -> None:
    st.subheader("Партнёры")
    with st.expander("Добавить партнёра", expanded=False):
        with st.form("crm_partner_form"):
            name = st.text_input("Имя")
            company = st.text_input("Компания")
            specialty = st.text_input("Специализация")
            niches = st.text_input("Ниши")
            contacts = st.columns(2)
            email = contacts[0].text_input("Email")
            telegram = contacts[1].text_input("Telegram")
            state = st.selectbox("Состояние", PARTNER_STATES)
            evidence = st.text_area("Доказательство знакомства")
            terms = st.columns(2)
            commission_percent = terms[0].number_input("Комиссия, %", 0, 100, 10)
            delay = terms[1].number_input("Срок выплаты после полной оплаты, дней", 0, 365, 3)
            notes = st.text_area("Условия и заметки")
            submitted = st.form_submit_button("Добавить партнёра")
        if submitted:
            _show_error(lambda: store.create_partner(
                name, evidence, company=company, specialty=specialty, niches=niches, email=email,
                telegram=telegram, state=state, default_commission_bps=int(commission_percent) * 100,
                payout_delay_days=int(delay), notes=notes,
            ))
    partners = store.list_partners()
    _table(partners, [
        "id", "name", "company", "specialty", "niches", "state", "default_commission_bps",
        "payout_delay_days", "referrals", "won_deals", "relationship_evidence"
    ])
    if partners:
        partner_by_id = {int(row["id"]): row for row in partners}
        state_columns = st.columns(3)
        partner_to_update = state_columns[0].selectbox(
            "Партнёр для изменения", list(partner_by_id),
            format_func=lambda key: partner_by_id[key]["name"], key="crm_partner_state_id"
        )
        partner_state = state_columns[1].selectbox(
            "Новое состояние", PARTNER_STATES, key="crm_partner_state_value"
        )
        if state_columns[2].button("Изменить состояние", key="crm_partner_state_save"):
            _show_error(lambda: store.set_partner_state(partner_to_update, partner_state))

    st.subheader("Рекомендации")
    if not partners or not leads:
        st.caption("Для рекомендации нужны партнёр и сохранённый лид.")
    else:
        partner_by_id = {int(row["id"]): row for row in partners}
        lead_by_key = {lead.lead_key: lead for lead in leads}
        with st.form("crm_referral_form"):
            partner_id = st.selectbox(
                "Партнёр", list(partner_by_id), format_func=lambda key: partner_by_id[key]["name"]
            )
            lead_key = st.selectbox(
                "Лид", list(lead_by_key), format_func=lambda key: _lead_label(lead_by_key[key])
            )
            channel = st.text_input("Канал знакомства")
            evidence = st.text_area("Доказательство представления")
            deal_title = st.text_input("Предмет сделки", value="Разработка сайта")
            submitted = st.form_submit_button("Создать рекомендацию и сделку")
        if submitted:
            _show_error(lambda: store.create_referral(
                partner_id, lead_key, channel, evidence, deal_title=deal_title
            ))
            st.caption("Разрешения контакта не изменяются: их нужно подтвердить отдельно.")
    _table(store.list_referrals(), [
        "id", "introduced_at", "partner_name", "lead_name", "channel", "commission_bps", "deal_id", "stage", "evidence"
    ])


def _render_deals(store: CRMStore, leads: list[Lead]) -> None:
    st.subheader("Единая воронка")
    deals = store.list_deals()
    stage_columns = st.columns(len(DEAL_STAGES))
    for column, stage in zip(stage_columns, DEAL_STAGES):
        stage_deals = [deal for deal in deals if deal["stage"] == stage]
        column.metric(STAGE_LABELS[stage], len(stage_deals))
        for deal in stage_deals[:5]:
            column.caption(f"#{deal['id']} {deal['lead_name']}\n\n{deal['title']}")

    with st.expander("Создать сделку", expanded=False):
        if not leads:
            st.caption("Нет сохранённых лидов.")
        else:
            lead_by_key = {lead.lead_key: lead for lead in leads}
            with st.form("crm_deal_form"):
                lead_key = st.selectbox(
                    "Лид", list(lead_by_key), format_func=lambda key: _lead_label(lead_by_key[key])
                )
                title = st.text_input("Предмет сделки", value="Разработка сайта")
                source_kind = st.selectbox("Источник", SOURCE_KINDS, format_func=SOURCE_LABELS.get)
                submitted = st.form_submit_button("Создать сделку")
            if submitted:
                _show_error(lambda: store.create_deal(lead_key, title, source_kind=source_kind))

    if not deals:
        st.caption("Сделок пока нет.")
        return
    deal_by_id = {int(row["id"]): row for row in deals}
    deal_id = st.selectbox(
        "Карточка сделки", list(deal_by_id),
        format_func=lambda key: f"#{key} — {deal_by_id[key]['lead_name']} / {deal_by_id[key]['title']}"
    )
    deal = deal_by_id[deal_id]
    details = st.columns(4)
    details[0].metric("Этап", STAGE_LABELS[str(deal["stage"])])
    details[1].metric("Стоимость", _rubles(deal["value_kopecks"]))
    details[2].metric("Оплачено", _rubles(deal["paid_kopecks"]))
    details[3].metric("Источник", SOURCE_LABELS.get(str(deal["source_kind"]), deal["source_kind"]))

    next_stages = sorted(DEAL_TRANSITIONS[str(deal["stage"])], key=DEAL_STAGES.index)
    if next_stages:
        transition_columns = st.columns(3)
        next_stage = transition_columns[0].selectbox(
            "Следующий этап", next_stages, format_func=STAGE_LABELS.get, key=f"crm_next_stage_{deal_id}"
        )
        value = transition_columns[1].text_input(
            "Стоимость, ₽", value="" if not deal["value_kopecks"] else str(Decimal(int(deal["value_kopecks"])) / 100),
            key=f"crm_deal_value_{deal_id}"
        )
        reason = transition_columns[2].text_input("Причина проигрыша", key=f"crm_lost_reason_{deal_id}")
        if st.button("Перевести сделку", key=f"crm_transition_{deal_id}"):
            def transition() -> None:
                kopecks = _kopecks(value) if value.strip() else None
                store.transition_deal(deal_id, next_stage, value_kopecks=kopecks, reason=reason)
            _show_error(transition)

    detail_tabs = st.tabs(["История", "Задачи", "Квалификация и возражения", "Документы"])
    with detail_tabs[0]:
        _table(store.list_stage_history(deal_id), ["changed_at", "from_stage", "to_stage", "reason"])
    with detail_tabs[1]:
        with st.form(f"crm_task_form_{deal_id}"):
            task_type = st.selectbox("Тип", ["qualify", "follow_up", "prepare_demo", "send_proposal", "meeting", "other"])
            description = st.text_input("Задача")
            due_date = st.date_input("Срок")
            submitted = st.form_submit_button("Добавить задачу")
        if submitted:
            _show_error(lambda: store.add_task(deal_id, description, task_type=task_type, due_at=_date_start(due_date)))
        tasks = store.list_tasks(deal_id)
        open_tasks = [row for row in tasks if row["status"] == "open"]
        if open_tasks:
            task_by_id = {int(row["id"]): row for row in open_tasks}
            task_id = st.selectbox(
                "Открытая задача", list(task_by_id), format_func=lambda key: task_by_id[key]["description"]
            )
            if st.button("Отметить выполненной", key=f"crm_task_done_{deal_id}"):
                _show_error(lambda: store.set_task_status(task_id, "done"))
        _table(tasks, ["id", "task_type", "description", "due_at", "status", "completed_at"])
    with detail_tabs[2]:
        with st.form(f"crm_note_form_{deal_id}"):
            note_type = st.selectbox("Тип записи", NOTE_TYPES)
            summary = st.text_area("Короткое резюме")
            evidence = st.text_input("Доказательство наблюдения")
            submitted = st.form_submit_button("Сохранить запись")
        if submitted:
            _show_error(lambda: store.add_deal_note(deal_id, note_type, summary, evidence))
        _table(store.list_deal_notes(deal_id), ["created_at", "note_type", "summary", "evidence"])
    with detail_tabs[3]:
        with st.form(f"crm_document_form_{deal_id}"):
            document_type = st.selectbox("Тип", ["proposal", "contract", "invoice", "act", "other"])
            number = st.text_input("Номер")
            url = st.text_input("Ссылка на документ")
            status = st.selectbox("Статус", ["draft", "sent", "signed", "paid", "cancelled"])
            submitted = st.form_submit_button("Сохранить реквизиты")
        if submitted:
            _show_error(lambda: store.add_document(
                deal_id, document_type, number=number, url=url, status=status
            ))
        _table(store.list_documents(deal_id), ["created_at", "document_type", "number", "url", "status"])


def _render_finance(store: CRMStore) -> None:
    store.refresh_payouts()
    metrics = store.metrics()
    columns = st.columns(4)
    columns[0].metric("Выиграно", _rubles(metrics["won_kopecks"]))
    columns[1].metric("Комиссии due", _rubles(metrics["commission_due_kopecks"]))
    columns[2].metric("Комиссии paid", _rubles(metrics["commission_paid_kopecks"]))
    columns[3].metric("Рекомендации", metrics["referrals"])

    st.subheader("Клиентские платежи")
    won_deals = [deal for deal in store.list_deals() if deal["stage"] == "won"]
    if won_deals:
        deal_by_id = {int(row["id"]): row for row in won_deals}
        with st.form("crm_payment_form"):
            deal_id = st.selectbox(
                "Сделка", list(deal_by_id),
                format_func=lambda key: f"#{key} — {deal_by_id[key]['lead_name']} / {deal_by_id[key]['title']}"
            )
            amount = st.text_input("Сумма, ₽")
            status = st.selectbox("Статус", ["planned", "paid", "cancelled"])
            reference = st.text_input("Номер/ссылка на подтверждение")
            submitted = st.form_submit_button("Добавить платёж")
        if submitted:
            _show_error(lambda: store.add_payment(
                deal_id, _kopecks(amount), status=status, external_ref=reference
            ))
    else:
        st.caption("Оплаченные платежи фиксируются после перевода сделки в won.")
    _table(store.list_payments(), [
        "id", "created_at", "lead_name", "title", "amount_kopecks", "status", "due_at", "paid_at", "external_ref"
    ])

    st.subheader("Партнёрские комиссии")
    payouts = store.list_payouts()
    due = [row for row in payouts if row["status"] == "due"]
    if due:
        payout_by_id = {int(row["id"]): row for row in due}
        payout_id = st.selectbox(
            "Комиссия к ручной выплате", list(payout_by_id),
            format_func=lambda key: f"#{key} — {payout_by_id[key]['partner_name']} / {_rubles(payout_by_id[key]['amount_kopecks'])}"
        )
        if st.button("Подтвердить ручную выплату", key="crm_payout_paid"):
            _show_error(lambda: store.mark_payout_paid(payout_id))
    _table(payouts, [
        "id", "partner_name", "lead_name", "title", "commission_bps", "basis_kopecks", "amount_kopecks",
        "due_at", "status", "paid_at"
    ])

    st.subheader("Конверсия по источникам")
    _table(store.source_conversion())
    st.download_button(
        "Экспортировать финансы в CSV",
        data=store.financial_export_csv(),
        file_name="crm_finance.csv",
        mime="text/csv",
        help="Отдельное явное действие. Контакты и тексты переписки в файл не входят.",
    )


def render_crm_section(db_path: str, leads: list[Lead]) -> None:
    store = CRMStore(db_path)
    metrics = store.metrics()
    summary = st.columns(6)
    summary[0].metric("Активные площадки", metrics["active_sources"])
    summary[1].metric("Полезные комментарии", metrics["useful_comments"])
    summary[2].metric("Входящие ответы", metrics["inbound_replies"])
    summary[3].metric("Активные партнёры", metrics["active_partners"])
    summary[4].metric("Квалифицированные сделки", metrics["qualified_deals"])
    summary[5].metric("Выиграно", _rubles(metrics["won_kopecks"]))
    platform_tab, partner_tab, deal_tab, finance_tab = st.tabs(
        ["Площадки", "Партнёры", "Сделки", "Финансы"]
    )
    with platform_tab:
        _render_platforms(store, leads)
    with partner_tab:
        _render_partners(store, leads)
    with deal_tab:
        _render_deals(store, leads)
    with finance_tab:
        _render_finance(store)
