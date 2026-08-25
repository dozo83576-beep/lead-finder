import unittest
from unittest.mock import Mock

import requests

from lead_finder import (
    PRESETS,
    Lead,
    WebsiteAudit,
    audit_website,
    build_overpass_query,
    collect_osm,
    deduplicate_leads,
    dry_run_leads,
    enrich_leads,
    filter_and_limit_leads,
    lead_queue,
    overpass_source_limit,
    parse_osm_elements,
    render_outreach,
    export_csv_bytes,
    sanitize_export_cell,
    strip_invisible_prefix,
    score_lead,
    score_components,
)


class FakeResponse:
    def __init__(self, status_code=200, text="", url="https://example.ru", payload=None):
        self.status_code = status_code
        self.text = text
        self.url = url
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class LeadFinderCoreTests(unittest.TestCase):
    def test_presets_contain_the_approved_ten_niches(self):
        self.assertEqual(
            set(PRESETS),
            {
                "Ремонт и отделка",
                "Сантехники",
                "Электрики",
                "Окна и двери",
                "Автосервисы",
                "Салоны красоты",
                "Стоматологии",
                "Юристы",
                "Бухгалтерия",
                "Клининг",
            },
        )

    def test_build_overpass_query_uses_preset_tags_keyword_and_limit(self):
        query = build_overpass_query(
            preset="Сантехники",
            keyword="аварийный",
            bbox=(56.7, 60.4, 56.9, 60.8),
            limit=50,
        )

        self.assertIn('["craft"="plumber"]', query)
        self.assertIn('["name"~"аварийный",i]', query)
        self.assertIn("out center 50", query)

    def test_overpass_source_limit_has_minimum_and_cap(self):
        self.assertEqual(overpass_source_limit(1), 200)
        self.assertEqual(overpass_source_limit(50), 250)
        self.assertEqual(overpass_source_limit(200), 1000)

    def test_parse_osm_elements_normalizes_contacts_and_stable_key(self):
        payload = {
            "elements": [
                {
                    "type": "node",
                    "id": 42,
                    "lat": 56.84,
                    "lon": 60.61,
                    "tags": {
                        "name": "Мастер окон",
                        "craft": "window_construction",
                        "addr:street": "Ленина",
                        "addr:housenumber": "10",
                        "contact:phone": "+7 343 000-00-01",
                        "contact:email": "hello@example.ru",
                        "contact:vk": "master_okon",
                        "contact:website": "example.ru",
                    },
                },
                {"type": "node", "id": 43, "tags": {"craft": "plumber"}},
            ]
        }

        leads = parse_osm_elements(payload, city="Екатеринбург")

        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].lead_key, "osm:node:42")
        self.assertEqual(leads[0].address, "Ленина, 10")
        self.assertEqual(leads[0].phone, "+7 343 000-00-01")
        self.assertEqual(leads[0].email, "hello@example.ru")
        self.assertEqual(leads[0].social, "https://vk.com/master_okon")
        self.assertEqual(leads[0].website, "example.ru")
        self.assertEqual((leads[0].latitude, leads[0].longitude), (56.84, 60.61))
        self.assertEqual(leads[0].verification_status, "source_provided")

    def test_audit_reachable_site_reads_objective_html_signals(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            text=(
                "<html><head><title>Компания</title>"
                '<meta name="description" content="Описание">'
                '<meta name="viewport" content="width=device-width">'
                "</head></html>"
            )
        )

        audit = audit_website("example.ru", session=session)

        self.assertEqual(audit.state, "reachable")
        self.assertTrue(audit.https)
        self.assertTrue(audit.mobile_viewport)
        self.assertTrue(audit.title_present)
        self.assertTrue(audit.description_present)

    def test_audit_403_is_limited_not_broken(self):
        session = Mock()
        session.get.return_value = FakeResponse(status_code=403)

        audit = audit_website("https://example.ru", session=session)

        self.assertEqual(audit.state, "limited")
        self.assertEqual(audit.status, 403)

    def test_audit_timeout_is_unknown_without_penalty(self):
        session = Mock()
        session.get.side_effect = requests.Timeout()

        audit = audit_website("https://example.ru", session=session)
        score, reasons = score_lead(Lead(lead_key="x", name="X"), audit)

        self.assertEqual(audit.state, "unknown")
        self.assertEqual(score, 0)
        self.assertIn("сайт не удалось проверить", reasons)

    def test_score_combines_need_and_contactability_with_cap(self):
        lead = Lead(
            lead_key="osm:node:1",
            name="Мастер окон",
            phone="+7",
            email="hello@example.ru",
            social="https://vk.com/master",
            verification_status="confirmed_no_site",
        )

        score, reasons = score_lead(lead, WebsiteAudit(state="missing"))

        self.assertEqual(score, 100)
        self.assertIn("отсутствие сайта подтверждено", reasons)
        self.assertIn("есть телефон", reasons)
        self.assertIn("есть email", reasons)

    def test_working_site_uses_only_confirmed_technical_problems(self):
        lead = Lead(lead_key="x", name="Компания", phone="+7")
        audit = WebsiteAudit(
            state="reachable",
            normalized_url="http://example.ru",
            https=False,
            mobile_viewport=False,
            title_present=False,
            description_present=False,
            mobile_score=42,
        )

        score, reasons = score_lead(lead, audit)

        self.assertEqual(score, 70)
        self.assertEqual(
            reasons,
            [
                "нет HTTPS",
                "нет мобильной адаптации",
                "нет title",
                "нет description",
                "низкая мобильная скорость",
                "есть телефон",
            ],
        )

    def test_outreach_has_three_channels_and_uses_confirmed_reason(self):
        lead = Lead(lead_key="x", name="Мастер окон", city="Екатеринбург", phone="+7")
        audit = WebsiteAudit(state="missing")

        message = render_outreach(lead, audit, "message")
        email = render_outreach(lead, audit, "email")
        call = render_outreach(lead, audit, "call")

        self.assertIn("Мастер окон", message)
        self.assertIn("не указан сайт", message)
        self.assertIn("Тема:", email)
        self.assertIn("Екатеринбург", email)
        self.assertIn("в городе Екатеринбург", message)
        self.assertIn("Вопрос:", call)

    def test_dry_run_is_deterministic_and_contains_audits(self):
        first = dry_run_leads()
        second = dry_run_leads()

        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)
        self.assertTrue(all(lead.audit is not None for lead in first))

    def test_collect_osm_falls_back_to_second_endpoint_and_filters_contacts(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            payload=[{"boundingbox": ["56.7", "56.9", "60.4", "60.8"]}]
        )
        session.post.side_effect = [
            requests.Timeout(),
            FakeResponse(
                payload={
                    "elements": [
                        {"type": "node", "id": 1, "tags": {"name": "Без контактов", "craft": "plumber"}},
                        {
                            "type": "node",
                            "id": 2,
                            "tags": {"name": "С телефоном", "craft": "plumber", "phone": "+7"},
                        },
                    ]
                }
            ),
        ]

        leads = collect_osm(
            city="Екатеринбург",
            preset="Сантехники",
            keyword="",
            limit=50,
            only_with_contacts=True,
            session=session,
        )

        self.assertEqual([lead.name for lead in leads], ["С телефоном"])
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(session.get.call_args.kwargs["params"]["countrycodes"], "ru")

    def test_collect_osm_overfetches_before_final_filter(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            payload=[{"boundingbox": ["56.7", "56.9", "60.4", "60.8"]}]
        )
        session.post.return_value = FakeResponse(
            payload={
                "elements": [
                    {"type": "node", "id": 1, "tags": {"name": "Без контактов"}},
                    {"type": "node", "id": 2, "tags": {"name": "С телефоном", "phone": "+7"}},
                ]
            }
        )

        leads = collect_osm(
            city="Екатеринбург",
            preset="Сантехники",
            keyword="",
            limit=50,
            only_with_contacts=False,
            session=session,
        )

        query = session.post.call_args.kwargs["data"]["data"]
        self.assertIn("out center 250", query)
        self.assertEqual(len(leads), 2)

    def test_deduplication_merges_branches_by_domain_and_phone_name(self):
        leads = [
            Lead(name="Альфа Дент", lead_key="1", website="https://alpha.ru/one", phone="+7 343 111-11-11"),
            Lead(name="Альфа Дент", lead_key="2", website="https://www.alpha.ru/two", email="mail@alpha.ru"),
            Lead(name="Бета", lead_key="3", phone="8 (343) 222-22-22"),
            Lead(name="Бета", lead_key="4", phone="+7 343 222-22-22", social="https://vk.com/beta"),
        ]

        result = deduplicate_leads(leads)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].branch_count, 2)
        self.assertEqual(result[0].email, "mail@alpha.ru")
        self.assertEqual(result[1].branch_count, 2)
        self.assertEqual(result[1].social, "https://vk.com/beta")

    def test_deduplication_does_not_use_incomplete_phone(self):
        result = deduplicate_leads(
            [
                Lead(name="Компания", lead_key="1", phone="+7"),
                Lead(name="Компания", lead_key="2", phone="+7"),
            ]
        )

        self.assertEqual(len(result), 2)

    def test_contact_filter_runs_after_enrichment(self):
        leads = [
            Lead(name="Получил email", lead_key="1", email="mail@example.ru", score=60),
            Lead(name="Без контактов", lead_key="2", score=90),
        ]

        result = filter_and_limit_leads(leads, only_with_contacts=True, limit=10)

        self.assertEqual([lead.name for lead in result], ["Получил email"])

    def test_lead_queue_requires_verified_need_for_ready_list(self):
        self.assertEqual(
            lead_queue(Lead(name="A", verification_status="confirmed_no_site")),
            "ready",
        )
        self.assertEqual(
            lead_queue(Lead(name="B", verification_status="site_found", need_score=20)),
            "ready",
        )
        self.assertEqual(
            lead_queue(Lead(name="C", verification_status="likely_no_site", need_score=45)),
            "confirmation",
        )
        self.assertEqual(
            lead_queue(Lead(name="D", verification_status="source_provided", need_score=0)),
            "all",
        )

    def test_score_separates_need_and_contactability(self):
        lead = Lead(
            name="Компания",
            lead_key="1",
            phone="+7",
            email="mail@example.ru",
            social="https://vk.com/company",
            verification_status="confirmed_no_site",
        )

        need, contact, reasons = score_components(lead, WebsiteAudit(state="missing"))

        self.assertEqual((need, contact), (70, 30))
        self.assertIn("отсутствие сайта подтверждено", reasons)

    def test_likely_no_site_scores_lower_than_confirmed_absence(self):
        lead = Lead(name="Компания", lead_key="1", phone="+7", verification_status="likely_no_site")

        need, contact, _ = score_components(lead, WebsiteAudit(state="missing"))

        self.assertEqual((need, contact), (45, 20))

    def test_missing_customer_action_adds_need_score(self):
        lead = Lead(name="Компания", lead_key="1", website="https://example.ru")
        audit = WebsiteAudit(
            state="reachable",
            normalized_url="https://example.ru",
            https=True,
            mobile_viewport=True,
            title_present=True,
            description_present=True,
            contact_action=False,
        )

        need, contact, reasons = score_components(lead, audit)

        self.assertEqual((need, contact), (15, 0))
        self.assertIn("нет действия для клиента", reasons)

    def test_enrichment_crawls_contacts_without_overwriting_source_data(self):
        lead = Lead(name="Компания", lead_key="1", website="https://example.ru", phone="+7 source")

        def fake_audit(value, pagespeed_key=""):
            return WebsiteAudit(
                state="reachable",
                normalized_url=value,
                https=True,
                mobile_viewport=True,
                title_present=True,
                description_present=True,
            )

        def fake_crawl(value):
            return {
                "phone": "+7 crawl",
                "email": "mail@example.ru",
                "social": "https://vk.com/company",
                "has_form": True,
                "contact_page": "https://example.ru/contacts",
                "online_booking": True,
            }

        result = enrich_leads([lead], audit_func=fake_audit, crawl_func=fake_crawl)[0]

        self.assertEqual(result.phone, "+7 source")
        self.assertEqual(result.email, "mail@example.ru")
        self.assertEqual(result.social, "https://vk.com/company")
        self.assertTrue(result.audit.contact_action)
        self.assertIn("найдена онлайн-запись", result.verification_evidence)

    def test_collect_osm_wraps_geocoding_failure_in_readable_error(self):
        session = Mock()
        session.get.side_effect = requests.Timeout()

        with self.assertRaisesRegex(RuntimeError, "Не удалось определить границы города"):
            collect_osm(
                city="Екатеринбург",
                preset="Сантехники",
                keyword="",
                session=session,
            )

    def test_enrich_leads_audits_scores_and_sorts(self):
        leads = [
            Lead(name="С сайтом", lead_key="1", website="https://example.ru", phone="+7"),
            Lead(name="Без сайта", lead_key="2", phone="+7", verification_status="likely_no_site"),
        ]

        def fake_audit(value, pagespeed_key=""):
            if value:
                return WebsiteAudit(
                    state="reachable",
                    normalized_url=value,
                    https=True,
                    mobile_viewport=True,
                    title_present=True,
                    description_present=True,
                )
            return WebsiteAudit(state="missing")

        result = enrich_leads(leads, audit_func=fake_audit)

        self.assertEqual([lead.name for lead in result], ["Без сайта", "С сайтом"])
        self.assertEqual(result[0].score, 65)
        self.assertEqual(result[1].score, 20)

    def test_enrich_leads_treats_social_profile_as_social_only(self):
        lead = Lead(name="Компания", lead_key="1", social="https://vk.com/company")

        result = enrich_leads([lead])

        self.assertEqual(result[0].audit.state, "social")
        self.assertEqual(result[0].score, 55)

    def test_missing_website_message_does_not_claim_proven_absence(self):
        lead = Lead(name="Компания", lead_key="1", source="OpenStreetMap")

        text = render_outreach(lead, WebsiteAudit(state="missing"), "message")

        self.assertIn("в открытой карточке не указан сайт", text)


    def test_fullwidth_formula_triggers_are_escaped(self):
        # Полноширинные варианты триггеров экранируем независимо от локали читателя.
        for trigger in ("＝", "＋", "－", "＠"):
            payload = trigger + "cmd|'/c calc'!A1"
            self.assertTrue(sanitize_export_cell(payload).startswith("'"), payload)

        # Невидимый префикс вместе с полноширинным триггером — тоже атака.
        combined = "​＝cmd|calc"
        self.assertTrue(sanitize_export_cell(combined).startswith("'"))

        # Надстрочный и типографский минус NFKC сводит к U+2212 — тоже триггер.
        for trigger in ("⁻", "₋", "−"):
            payload = trigger + "cmd|'/c calc'!A1"
            self.assertTrue(sanitize_export_cell(payload).startswith("'"), payload)

        # Числа и телефоны формулой не становятся ни в полноширинной записи,
        # ни с типографским минусом, который NFKC оставляет как U+2212.
        for value in ("＋７ 343 000-00-01", "－12,5", "−1 234,56", "⁻12,5"):
            self.assertEqual(sanitize_export_cell(value), value)

    def test_invisible_prefix_is_stripped_by_property_not_by_codepoint_list(self):
        import unicodedata

        # Берём произвольные символы категории Cf, которых нет ни в одном ручном списке.
        exotic = [chr(code) for code in (0x00ad, 0x061c, 0x180e, 0x2064, 0x2069, 0xe0041)]
        self.assertTrue(all(unicodedata.category(char) == "Cf" for char in exotic))
        for char in exotic:
            self.assertEqual(strip_invisible_prefix(char + "=cmd"), "=cmd")
            self.assertTrue(sanitize_export_cell(char + "=cmd").startswith("'"))

        self.assertEqual(strip_invisible_prefix("   "), "")
        self.assertEqual(strip_invisible_prefix("Мастер окон"), "Мастер окон")

    def test_export_neutralizes_formula_injection_but_keeps_phones(self):
        hostile = Lead(
            name="=cmd|'/c calc'!A1",
            lead_key="hostile",
            phone="+79001234567",
            city="@SUM(1+1)",
        )

        text = export_csv_bytes([hostile]).decode("utf-8-sig")

        self.assertIn("'=cmd|'/c calc'!A1", text)
        self.assertIn("'@SUM(1+1)", text)
        self.assertIn("+79001234567", text)
        self.assertNotIn("'+79001234567", text)
        self.assertEqual(sanitize_export_cell("-12.5"), "-12.5")
        self.assertEqual(sanitize_export_cell("обычное имя"), "обычное имя")
        self.assertEqual(sanitize_export_cell(42), 42)

    def test_export_keeps_real_phone_formats_and_catches_leading_space(self):
        # Именно такой формат телефона отдаёт собственный Dry Run приложения.
        for phone in ("+7 343 000-00-01", "+7 (343) 000-00-01", "8-800-555-35-35", "-1 234,56"):
            self.assertEqual(sanitize_export_cell(phone), phone)

        # Google Sheets и LibreOffice отбрасывают ведущий пробел, формула оживает.
        # Неразрывный пробел, символы нулевой ширины и маркеры направления письма
        # прячут формулу от сравнения первого символа — снимаем и их.
        for attack in (
            " =cmd|'/c calc'!A1",
            "\t=HYPERLINK(\"http://evil\")",
            "  @SUM(1+1)",
            " =cmd|'/c calc'!A1",
            " @SUM(1+1)",
            "​=cmd|'/c calc'!A1",
            "‏=HYPERLINK(\"http://evil\")",
            "﻿@SUM(1+1)",
            "‮=cmd|'/c calc'!A1",
            "⁦@SUM(1+1)",
            "­=cmd|calc",
            "؜=cmd|calc",
            "⁡=cmd|calc",
            "󠀠=cmd|calc",
            " ​‮­ =cmd|calc",
        ):
            self.assertTrue(sanitize_export_cell(attack).startswith("'"), attack)


if __name__ == "__main__":
    unittest.main()
