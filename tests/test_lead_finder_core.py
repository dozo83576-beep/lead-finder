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
    dry_run_leads,
    enrich_leads,
    parse_osm_elements,
    render_outreach,
    score_lead,
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

    def test_parse_osm_elements_normalizes_contacts_and_stable_key(self):
        payload = {
            "elements": [
                {
                    "type": "node",
                    "id": 42,
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
        )

        score, reasons = score_lead(lead, WebsiteAudit(state="missing"))

        self.assertEqual(score, 90)
        self.assertIn("сайт не указан в источнике", reasons)
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
            Lead(name="Без сайта", lead_key="2", phone="+7"),
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
        self.assertEqual(result[0].score, 80)
        self.assertEqual(result[1].score, 20)

    def test_enrich_leads_treats_social_profile_as_social_only(self):
        lead = Lead(name="Компания", lead_key="1", social="https://vk.com/company")

        result = enrich_leads([lead])

        self.assertEqual(result[0].audit.state, "social")
        self.assertEqual(result[0].score, 50)

    def test_missing_website_message_does_not_claim_proven_absence(self):
        lead = Lead(name="Компания", lead_key="1", source="OpenStreetMap")

        text = render_outreach(lead, WebsiteAudit(state="missing"), "message")

        self.assertIn("в открытой карточке не указан сайт", text)


if __name__ == "__main__":
    unittest.main()
