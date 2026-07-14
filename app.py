import logging
import os
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

from lead_finder import (
    PRESETS,
    STATUSES,
    Lead,
    WebsiteAudit,
    collect_osm,
    configure_logging,
    dry_run_leads,
    enrich_leads,
    export_csv_bytes,
    export_xlsx_bytes,
    filter_and_limit_leads,
    import_csv,
    import_xlsx,
    lead_queue,
    normalize_website,
    render_outreach,
    resolve_city_bbox,
    score_components,
)
from storage import LeadStore
from verification import (
    calculate_request_allowance,
    check_yandex_connection,
    crawl_contacts,
    estimated_cost,
    manual_yandex_search_url,
    verify_lead_site,
    verify_missing_leads,
)


VERIFICATION_LABELS = {
    "source_provided": "Сайт указан в источнике",
    "site_found": "Сайт найден и сопоставлен",
    "likely_no_site": "Сайт не найден — подтвердите вручную",
    "confirmed_no_site": "Отсутствие сайта подтверждено",
    "ambiguous": "Требуется ручная проверка",
    "verification_error": "Ошибка автоматической проверки",
}


def lead_table(leads: list[Lead]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Балл": lead.score,
                "Потребность": lead.need_score,
                "Доступность": lead.contact_score,
                "Компания": lead.name,
                "Проверка": VERIFICATION_LABELS.get(lead.verification_status, lead.verification_status),
                "Телефон": lead.phone,
                "Email": lead.email,
                "Филиалов": lead.branch_count,
                "Статус": lead.status,
            }
            for lead in leads
        ]
    )


def website_host(value: str) -> str:
    normalized = normalize_website(value).normalized_url
    return urlparse(normalized).netloc.lower().split(":", 1)[0].removeprefix("www.")


st.set_page_config(page_title="Lead Finder", page_icon="🔎", layout="wide")
configure_logging(os.environ.get("LEAD_LOG_PATH", "lead_finder.log"))
store = LeadStore(os.environ.get("LEAD_DB_PATH", "leads.db"))

st.title("Lead Finder")
st.caption("Поиск проверенных компаний без сайта или с подтверждёнными проблемами сайта")

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

    with st.expander("Бюджет Yandex Search API"):
        yandex_api_key = os.environ.get("YANDEX_SEARCH_API_KEY", "")
        yandex_folder_id = os.environ.get("YANDEX_FOLDER_ID", "")
        monthly_budget = st.number_input(
            "Бюджет в месяц, ₽",
            min_value=0.0,
            value=float(os.environ.get("YANDEX_MONTHLY_BUDGET", "300")),
            step=50.0,
            key="monthly_budget",
        )
        price_per_1000 = st.number_input(
            "Цена за 1000 запросов, ₽",
            min_value=0.01,
            value=float(os.environ.get("YANDEX_PRICE_PER_1000", "488")),
            step=1.0,
            key="price_per_1000",
        )
        used_requests = store.monthly_yandex_requests()
        allowed_requests = calculate_request_allowance(
            monthly_budget, price_per_1000, used_requests, int(limit)
        )
        st.caption(
            f"Использовано в этом месяце: {used_requests}. "
            f"Следующий запуск: до {allowed_requests} запросов, около "
            f"{estimated_cost(allowed_requests, price_per_1000):.2f} ₽."
        )
        yandex_configured = bool(yandex_api_key and yandex_folder_id)
        if not yandex_configured:
            st.info("Ключи Яндекса не настроены: останется ручная проверка.")
        st.caption(
            "Проверка подключения выполняет один тарифицируемый запрос, "
            "который не учитывается внутренним месячным счётчиком."
        )
        if st.button(
            "Проверить подключение к Yandex API",
            key="check_yandex_connection",
            disabled=not yandex_configured or dry_run,
            width="stretch",
        ):
            ok, message, _ = check_yandex_connection(yandex_api_key, yandex_folder_id)
            logging.info("Проверка подключения Yandex Search API: %s.", "успешно" if ok else "ошибка")
            st.success(message) if ok else st.error(message)

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
            bbox = store.get_city_bbox(city)
            if bbox is None:
                bbox = resolve_city_bbox(city)
                store.save_city_bbox(city, bbox)
                logging.info("Границы города сохранены в кэш на 30 дней: %s.", city)
            found = collect_osm(
                city,
                preset,
                keyword,
                int(limit),
                only_with_contacts=False,
                bbox=bbox,
            )
            ranked = sorted(
                found,
                key=lambda lead: bool(lead.phone or lead.email or lead.social or lead.website),
                reverse=True,
            )
            enriched = enrich_leads(
                ranked,
                pagespeed_key=os.environ.get("PAGESPEED_API_KEY", ""),
                crawl_func=crawl_contacts,
            )
            verified, api_stats = verify_missing_leads(
                enriched,
                yandex_api_key,
                yandex_folder_id,
                max_requests=allowed_requests,
            )
            for lead in verified:
                if lead.audit is not None:
                    lead.need_score, lead.contact_score, lead.reasons = score_components(
                        lead, lead.audit
                    )
                    lead.score = min(100, lead.need_score + lead.contact_score)
            changed_sites = [lead for lead in verified if lead.website and lead.audit is None]
            if changed_sites:
                enrich_leads(
                    changed_sites,
                    pagespeed_key=os.environ.get("PAGESPEED_API_KEY", ""),
                    crawl_func=crawl_contacts,
                )
            final_leads = filter_and_limit_leads(verified, only_contacts, int(limit))
            store.upsert_many(final_leads)
            ready_count = sum(lead_queue(lead) == "ready" for lead in final_leads)
            run_cost = estimated_cost(api_stats["api_requests"], price_per_1000)
            store.record_search_run(
                city=city,
                preset=preset,
                osm_found=len(found),
                yandex_checked=api_stats["yandex_checked"],
                sites_found=api_stats["sites_found"],
                ready_leads=ready_count,
                api_requests=api_stats["api_requests"],
                estimated_cost=run_cost,
            )
            st.session_state.leads = store.list_leads()
            st.session_state.dry_results = False
            logging.info(
                "Поиск завершён: OSM=%s, Яндекс=%s, сайты=%s, готовые=%s, стоимость=%.3f ₽.",
                len(found),
                api_stats["yandex_checked"],
                api_stats["sites_found"],
                ready_count,
                run_cost,
            )
            st.success(f"Готовых лидов: {ready_count}. Проверка Яндекса: {run_cost:.2f} ₽.")
    except (OSError, ValueError, RuntimeError) as error:
        logging.error("Поиск не выполнен: %s", error)
        st.error(str(error))

if import_clicked:
    if uploaded is None:
        st.warning("Выберите CSV или XLSX.")
    else:
        try:
            imported = import_csv(uploaded) if uploaded.name.lower().endswith(".csv") else import_xlsx(uploaded)
            enriched = enrich_leads(
                imported,
                pagespeed_key=os.environ.get("PAGESPEED_API_KEY", ""),
                crawl_func=crawl_contacts,
            )
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
ready_leads = [lead for lead in leads if lead_queue(lead) == "ready"]
confirmation_leads = [lead for lead in leads if lead_queue(lead) == "confirmation"]

metric_columns = st.columns(5)
metric_columns[0].metric("Всего", len(leads))
metric_columns[1].metric("Готовые", len(ready_leads))
metric_columns[2].metric("Требуют подтверждения", len(confirmation_leads))
metric_columns[3].metric("Найдены сайты", sum(lead.verification_status == "site_found" for lead in leads))
metric_columns[4].metric("Связались", sum(lead.status != "Новый" for lead in leads))

if not leads:
    st.info("Лидов пока нет. Используйте поиск, импорт или Dry Run.")
    st.markdown("Данные: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)")
    st.stop()

ready_tab, confirmation_tab, all_tab = st.tabs(["Готовые лиды", "Требуют подтверждения", "Все"])
with ready_tab:
    if ready_leads:
        st.dataframe(lead_table(ready_leads), hide_index=True, width="stretch")
    else:
        st.info("Нет лидов с подтверждённой потребностью.")
with confirmation_tab:
    if confirmation_leads:
        st.dataframe(lead_table(confirmation_leads), hide_index=True, width="stretch")
    else:
        st.info("Очередь ручной проверки пуста.")
with all_tab:
    st.dataframe(lead_table(leads), hide_index=True, width="stretch")

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
score_columns = st.columns(3)
score_columns[0].metric("Общий балл", selected.score)
score_columns[1].metric("Потребность", selected.need_score)
score_columns[2].metric("Доступность", selected.contact_score)
st.write("**Проверка:** " + VERIFICATION_LABELS.get(selected.verification_status, selected.verification_status))
st.write("**Причины приоритета:** " + ("; ".join(selected.reasons) or "подтверждённых проблем нет"))
if selected.verification_evidence:
    st.write("**Доказательства:** " + "; ".join(selected.verification_evidence))
contact_parts = [part for part in (selected.phone, selected.email, selected.social, selected.website) if part]
st.write("**Контакты:** " + (" · ".join(contact_parts) if contact_parts else "не указаны"))
if selected.source_url:
    st.link_button("Открыть источник", selected.source_url)
st.link_button("Открыть ручной поиск в Яндексе", manual_yandex_search_url(selected))

if not st.session_state.dry_results:
    verification_columns = st.columns([1, 2])
    if verification_columns[0].button(
        "Подтвердить отсутствие сайта", key=f"confirm_no_site_{selected.lead_key}", width="stretch"
    ):
        selected.website = ""
        selected.website_source = ""
        selected.verification_status = "confirmed_no_site"
        selected.verification_evidence = ["отсутствие сайта подтверждено вручную"]
        selected.audit = WebsiteAudit(state="missing")
        selected.need_score, selected.contact_score, selected.reasons = score_components(selected, selected.audit)
        selected.score = min(100, selected.need_score + selected.contact_score)
        store.upsert_many([selected])
        st.session_state.leads = store.list_leads()
        st.success("Отсутствие сайта подтверждено.")

    manual_site = verification_columns[1].text_input(
        "Указать найденный сайт", key=f"manual_site_{selected.lead_key}", placeholder="https://example.ru"
    )
    if verification_columns[1].button("Сохранить и проверить сайт", key=f"save_site_{selected.lead_key}"):
        normalized = normalize_website(manual_site)
        if normalized.state in ("missing", "broken") and normalized.error:
            st.error("Укажите корректный адрес сайта.")
        elif normalized.state == "social":
            st.error("Укажите официальный сайт, а не страницу в соцсети.")
        else:
            selected.website = normalized.normalized_url
            selected.website_source = "Ручная проверка"
            selected.verification_status = "site_found"
            selected.verification_evidence = ["сайт указан вручную"]
            updated = enrich_leads(
                [selected],
                pagespeed_key=os.environ.get("PAGESPEED_API_KEY", ""),
                crawl_func=crawl_contacts,
            )[0]
            store.upsert_many([updated])
            st.session_state.leads = store.list_leads()
            st.success("Сайт сохранён и проверен.")

    if selected.website:
        manual_request_allowed = calculate_request_allowance(
            monthly_budget,
            price_per_1000,
            store.monthly_yandex_requests(),
            1,
        )
        st.caption("Поиск актуального домена использует один запрос и учитывается в месячном бюджете.")
        if st.button(
            "Найти актуальный домен",
            key=f"find_current_site_{selected.lead_key}",
            disabled=not yandex_configured or manual_request_allowed == 0,
        ):
            old_website = selected.website
            old_source = selected.website_source
            old_status = selected.verification_status
            old_audit = selected.audit
            result = verify_lead_site(selected, yandex_api_key, yandex_folder_id)
            replaced = bool(
                result.website and website_host(result.website) != website_host(old_website)
            )
            if replaced:
                selected.website = result.website
                selected.website_source = "Yandex Search API"
                selected.verification_status = "site_found"
                selected.verification_evidence.extend(
                    [f"предыдущий сайт: {old_website}", *(result.evidence or [])]
                )
                selected.audit = None
                selected = enrich_leads(
                    [selected],
                    pagespeed_key=os.environ.get("PAGESPEED_API_KEY", ""),
                    crawl_func=crawl_contacts,
                )[0]
                st.success(f"Актуальный домен сохранён: {selected.website}")
            else:
                selected.website = old_website
                selected.website_source = old_source
                selected.verification_status = old_status
                selected.audit = old_audit
                selected.verification_evidence.extend(
                    item for item in (result.evidence or []) if item not in selected.verification_evidence
                )
                if result.website:
                    st.info("Яндекс подтвердил текущий домен; замена не требуется.")
                elif result.status == "verification_error":
                    st.error("Не удалось проверить актуальный домен.")
                else:
                    st.warning("Новый подтверждённый домен не найден.")
            store.upsert_many([selected])
            store.record_search_run(
                city=selected.city,
                preset="Ручная проверка домена",
                osm_found=0,
                yandex_checked=result.api_requests,
                sites_found=int(replaced),
                ready_leads=int(lead_queue(selected) == "ready"),
                api_requests=result.api_requests,
                estimated_cost=estimated_cost(result.api_requests, price_per_1000),
            )
            st.session_state.leads = store.list_leads()

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
    st.caption("Dry Run: статусы, проверки и заметки не записываются в базу.")

if selected.audit is not None:
    message_tab, email_tab, call_tab = st.tabs(["Мессенджер", "Email", "Звонок"])
    message_tab.code(render_outreach(selected, selected.audit, "message"), language=None, wrap_lines=True)
    email_tab.code(render_outreach(selected, selected.audit, "email"), language=None, wrap_lines=True)
    call_tab.code(render_outreach(selected, selected.audit, "call"), language=None, wrap_lines=True)

history = store.list_search_runs()
if history:
    with st.expander("История поисковых запусков"):
        history_table = pd.DataFrame(history).rename(
            columns={
                "created_at": "Дата",
                "city": "Город",
                "preset": "Ниша",
                "osm_found": "Найдено в OSM",
                "yandex_checked": "Проверено через Яндекс",
                "sites_found": "Найдены сайты",
                "ready_leads": "Готовые лиды",
                "api_requests": "API-запросы",
                "estimated_cost": "Стоимость, ₽",
            }
        )
        st.dataframe(history_table.drop(columns=["id"]), hide_index=True, width="stretch")

st.markdown("Данные: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)")
