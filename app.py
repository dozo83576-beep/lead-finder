import logging
import os

import pandas as pd
import streamlit as st

from lead_finder import (
    PRESETS,
    STATUSES,
    Lead,
    collect_osm,
    configure_logging,
    dry_run_leads,
    enrich_leads,
    export_csv_bytes,
    export_xlsx_bytes,
    import_csv,
    import_xlsx,
    render_outreach,
)
from storage import LeadStore


st.set_page_config(page_title="Lead Finder", page_icon="🔎", layout="wide")
configure_logging(os.environ.get("LEAD_LOG_PATH", "lead_finder.log"))
store = LeadStore(os.environ.get("LEAD_DB_PATH", "leads.db"))

st.title("Lead Finder")
st.caption("Поиск локальных компаний без сайта или с подтверждёнными техническими проблемами")

with st.sidebar:
    st.header("Новый поиск")
    city = st.text_input("Город", value="Екатеринбург", key="city")
    preset = st.selectbox("Ниша", [*PRESETS, "Другая ниша"], key="preset")
    keyword = st.text_input(
        "Ключевое слово",
        help="Обязательно для варианта «Другая ниша»; для пресета дополнительно сужает поиск.",
        key="keyword",
    )
    limit = st.number_input("Лимит", min_value=1, max_value=200, value=50, key="limit")
    only_contacts = st.checkbox("Только с контактами", value=True, key="only_contacts")
    dry_run = st.checkbox("Тестовый запуск без сети", value=False, key="dry_run")
    search_clicked = st.button("Найти и проверить", type="primary", key="search", width="stretch")

    st.divider()
    uploaded = st.file_uploader("Импорт CSV или XLSX", type=["csv", "xlsx"], key="upload")
    import_clicked = st.button("Импортировать и проверить", key="import", width="stretch")

    st.divider()
    status_filter = st.selectbox("Статус", ["Все", *STATUSES], key="status_filter")
    minimum_score = st.slider("Минимальный балл", 0, 100, 0, key="minimum_score")

if "leads" not in st.session_state:
    st.session_state.leads = store.list_leads()
if "dry_results" not in st.session_state:
    st.session_state.dry_results = False

if search_clicked:
    try:
        if dry_run:
            st.session_state.leads = dry_run_leads()
            st.session_state.dry_results = True
            logging.info("Dry Run: подготовлено %s тестовых лидов.", len(st.session_state.leads))
        else:
            logging.info("Поиск: город=%s, ниша=%s, лимит=%s.", city, preset, limit)
            found = collect_osm(city, preset, keyword, int(limit), only_contacts)
            enriched = enrich_leads(found, pagespeed_key=os.environ.get("PAGESPEED_API_KEY", ""))
            store.upsert_many(enriched)
            st.session_state.leads = store.list_leads()
            st.session_state.dry_results = False
            logging.info("Поиск завершён: сохранено %s лидов.", len(enriched))
            st.success(f"Найдено и проверено: {len(enriched)}")
    except (ValueError, RuntimeError) as error:
        logging.error("Поиск не выполнен: %s", error)
        st.error(str(error))

if import_clicked:
    if uploaded is None:
        st.warning("Выберите CSV или XLSX.")
    else:
        try:
            imported = import_csv(uploaded) if uploaded.name.lower().endswith(".csv") else import_xlsx(uploaded)
            enriched = enrich_leads(imported, pagespeed_key=os.environ.get("PAGESPEED_API_KEY", ""))
            store.upsert_many(enriched)
            st.session_state.leads = store.list_leads()
            st.session_state.dry_results = False
            logging.info("Импорт завершён: сохранено %s лидов.", len(enriched))
            st.success(f"Импортировано и проверено: {len(enriched)}")
        except (OSError, ValueError) as error:
            logging.error("Импорт не выполнен: %s", error)
            st.error(str(error))

leads: list[Lead] = [
    lead
    for lead in st.session_state.leads
    if lead.score >= minimum_score and (status_filter == "Все" or lead.status == status_filter)
]

metric_columns = st.columns(4)
metric_columns[0].metric("Лидов", len(leads))
metric_columns[1].metric("Без сайта", sum(lead.audit is not None and lead.audit.state == "missing" for lead in leads))
metric_columns[2].metric("С проблемами", sum(lead.score >= 45 for lead in leads))
metric_columns[3].metric("Связались", sum(lead.status != "Новый" for lead in leads))

if not leads:
    st.info("Лидов пока нет. Используйте поиск, импорт или Dry Run.")
    st.markdown("Данные: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)")
    st.stop()

table = pd.DataFrame(
    [
        {
            "Балл": lead.score,
            "Приоритет": lead.priority,
            "Компания": lead.name,
            "Причина": "; ".join(lead.reasons),
            "Телефон": lead.phone,
            "Email": lead.email,
            "Статус": lead.status,
        }
        for lead in leads
    ]
)
st.dataframe(table, hide_index=True, width="stretch")

download_columns = st.columns([1, 1, 4])
download_columns[0].download_button(
    "Скачать CSV",
    data=export_csv_bytes(leads),
    file_name="leads.csv",
    mime="text/csv",
    width="stretch",
)
download_columns[1].download_button(
    "Скачать XLSX",
    data=export_xlsx_bytes(leads),
    file_name="leads.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    width="stretch",
)

selected_key = st.selectbox(
    "Карточка лида",
    [lead.lead_key for lead in leads],
    format_func=lambda key: next(lead.name for lead in leads if lead.lead_key == key),
    key="selected_lead",
)
selected = next(lead for lead in leads if lead.lead_key == selected_key)

st.subheader(selected.name)
st.write(f"{selected.city} · {selected.category or 'категория не указана'}")
st.write("**Причины приоритета:** " + "; ".join(selected.reasons))
contact_parts = [part for part in (selected.phone, selected.email, selected.social, selected.website) if part]
st.write("**Контакты:** " + (" · ".join(contact_parts) if contact_parts else "не указаны"))
if selected.source_url:
    st.link_button("Открыть источник", selected.source_url)

if not st.session_state.dry_results:
    edit_columns = st.columns(2)
    status = edit_columns[0].selectbox(
        "Статус лида",
        STATUSES,
        index=STATUSES.index(selected.status),
        key=f"status_{selected.lead_key}",
    )
    note = edit_columns[1].text_area("Заметка", value=selected.note, key=f"note_{selected.lead_key}")
    if st.button("Сохранить статус и заметку", key="save_lead"):
        store.update_status(selected.lead_key, status)
        store.update_note(selected.lead_key, note)
        st.session_state.leads = store.list_leads()
        st.success("Изменения сохранены.")
else:
    st.caption("Dry Run: статусы и заметки не записываются в базу.")

if selected.audit is not None:
    message_tab, email_tab, call_tab = st.tabs(["Мессенджер", "Email", "Звонок"])
    message_tab.code(render_outreach(selected, selected.audit, "message"), language=None, wrap_lines=True)
    email_tab.code(render_outreach(selected, selected.audit, "email"), language=None, wrap_lines=True)
    call_tab.code(render_outreach(selected, selected.audit, "call"), language=None, wrap_lines=True)

st.markdown("Данные: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)")
