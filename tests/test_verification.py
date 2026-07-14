import base64
import unittest
from unittest.mock import Mock

import requests

from lead_finder import Lead, WebsiteAudit
from verification import (
    calculate_request_allowance,
    candidate_matches,
    check_yandex_connection,
    crawl_contacts,
    is_excluded_result,
    parse_yandex_xml,
    verify_lead_site,
    verify_missing_leads,
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


class VerificationTests(unittest.TestCase):
    def test_connection_check_requires_credentials_without_network(self):
        session = Mock()

        ok, message, api_requests = check_yandex_connection("", "", session=session)

        self.assertFalse(ok)
        self.assertIn("не настроен", message)
        self.assertEqual(api_requests, 0)
        session.post.assert_not_called()

    def test_connection_check_uses_one_request_without_exposing_key(self):
        xml = "<yandexsearch><response><results><grouping /></results></response></yandexsearch>"
        session = Mock()
        session.post.return_value = FakeResponse(
            payload={"rawData": base64.b64encode(xml.encode()).decode()}
        )

        ok, message, api_requests = check_yandex_connection(
            "secret-api-key", "folder", session=session
        )

        self.assertTrue(ok)
        self.assertIn("работает", message)
        self.assertNotIn("secret-api-key", message)
        self.assertEqual(api_requests, 1)
        self.assertEqual(session.post.call_count, 1)

    def test_connection_check_returns_safe_http_error(self):
        session = Mock()
        session.post.return_value = FakeResponse(status_code=403)

        ok, message, api_requests = check_yandex_connection(
            "secret-api-key", "folder", session=session
        )

        self.assertFalse(ok)
        self.assertIn("HTTP 403", message)
        self.assertNotIn("secret-api-key", message)
        self.assertEqual(api_requests, 1)

    def test_parses_yandex_xml_urls(self):
        xml = """
        <yandexsearch><response><results><grouping>
          <group><doc><url>https://catalog.ru/card</url></doc></group>
          <group><doc><url>https://company.ru/</url></doc></group>
        </grouping></results></response></yandexsearch>
        """

        self.assertEqual(parse_yandex_xml(xml), ["https://catalog.ru/card", "https://company.ru/"])

    def test_excludes_catalogs_maps_aggregators_and_socials(self):
        self.assertTrue(is_excluded_result("https://yandex.ru/maps/org/1"))
        self.assertTrue(is_excluded_result("https://2gis.ru/ekaterinburg/firm/1"))
        self.assertTrue(is_excluded_result("https://vk.com/company"))
        self.assertFalse(is_excluded_result("https://company.ru"))

    def test_candidate_matches_phone_or_name_with_location(self):
        lead = Lead(
            name="Эстетик Арт",
            city="Екатеринбург",
            address="Ленина, 10",
            phone="+7 (343) 222-11-00",
        )

        phone_match, phone_evidence = candidate_matches(lead, "Телефон: 8 343 222 11 00")
        name_match, name_evidence = candidate_matches(
            lead, "Стоматология Эстетик Арт, Екатеринбург. Адрес: Ленина, 10"
        )

        self.assertTrue(phone_match)
        self.assertIn("телефон", phone_evidence)
        self.assertTrue(name_match)
        self.assertIn("название", name_evidence)

    def test_incomplete_phone_is_not_matching_evidence(self):
        matched, evidence = candidate_matches(Lead(name="X", phone="+7"), "Номер заказа 7")

        self.assertFalse(matched)
        self.assertEqual(evidence, "")

    def test_monthly_budget_limits_requests(self):
        self.assertEqual(calculate_request_allowance(300, 488, used_requests=600, requested=50), 14)
        self.assertEqual(calculate_request_allowance(300, 488, used_requests=614, requested=50), 0)

    def test_yandex_finds_official_site_and_keeps_evidence(self):
        xml = "<yandexsearch><response><results><grouping><group><doc><url>https://alpha-dent.ru/</url></doc></group></grouping></results></response></yandexsearch>"
        session = Mock()
        session.post.return_value = FakeResponse(
            payload={"rawData": base64.b64encode(xml.encode()).decode()}
        )
        session.get.return_value = FakeResponse(
            text="Альфа Дент — стоматология в Екатеринбурге. Телефон +7 343 222-11-00",
            url="https://alpha-dent.ru/",
        )
        lead = Lead(name="Альфа Дент", city="Екатеринбург", phone="+7 343 222-11-00")

        result = verify_lead_site(lead, "key", "folder", session=session)

        self.assertEqual(result.status, "site_found")
        self.assertEqual(result.website, "https://alpha-dent.ru")
        self.assertEqual(result.api_requests, 1)
        self.assertTrue(result.evidence)

    def test_site_not_found_is_only_likely_no_site(self):
        xml = "<yandexsearch><response><results><grouping /></results></response></yandexsearch>"
        session = Mock()
        session.post.return_value = FakeResponse(
            payload={"rawData": base64.b64encode(xml.encode()).decode()}
        )

        result = verify_lead_site(Lead(name="Компания", city="Екатеринбург"), "key", "folder", session)

        self.assertEqual(result.status, "likely_no_site")
        self.assertEqual(result.website, "")

    def test_unavailable_first_candidate_does_not_hide_second_match(self):
        xml = """
        <yandexsearch><response><results><grouping>
          <group><doc><url>https://unavailable.ru/</url></doc></group>
          <group><doc><url>https://company.ru/</url></doc></group>
        </grouping></results></response></yandexsearch>
        """
        session = Mock()
        session.post.return_value = FakeResponse(
            payload={"rawData": base64.b64encode(xml.encode()).decode()}
        )
        session.get.side_effect = [
            requests.Timeout(),
            FakeResponse(text="Альфа Дент, Екатеринбург", url="https://company.ru/"),
        ]

        result = verify_lead_site(Lead(name="Альфа Дент", city="Екатеринбург"), "key", "folder", session)

        self.assertEqual(result.status, "site_found")
        self.assertEqual(result.website, "https://company.ru")

    def test_unverifiable_candidates_are_ambiguous_not_no_site(self):
        xml = "<yandexsearch><response><results><grouping><group><doc><url>https://blocked.ru/</url></doc></group></grouping></results></response></yandexsearch>"
        session = Mock()
        session.post.return_value = FakeResponse(
            payload={"rawData": base64.b64encode(xml.encode()).decode()}
        )
        session.get.return_value = FakeResponse(status_code=403, url="https://blocked.ru/")

        result = verify_lead_site(Lead(name="Компания", city="Екатеринбург"), "key", "folder", session)

        self.assertEqual(result.status, "ambiguous")

    def test_api_error_does_not_claim_site_absence(self):
        session = Mock()
        session.post.side_effect = requests.Timeout()

        result = verify_lead_site(Lead(name="Компания", city="Екатеринбург"), "key", "folder", session)

        self.assertEqual(result.status, "verification_error")
        self.assertEqual(result.api_requests, 1)

    def test_dry_run_never_calls_yandex(self):
        session = Mock()
        leads = [Lead(name="Компания", lead_key="1")]

        result, stats = verify_missing_leads(
            leads, "key", "folder", max_requests=10, dry_run=True, session=session
        )

        session.post.assert_not_called()
        self.assertEqual(stats["api_requests"], 0)
        self.assertEqual(result[0].verification_status, "ambiguous")

    def test_broken_osm_site_is_replaced_with_verified_current_domain(self):
        xml = "<yandexsearch><response><results><grouping><group><doc><url>https://www.ursula.pro/</url></doc></group></grouping></results></response></yandexsearch>"
        session = Mock()
        session.post.return_value = FakeResponse(
            payload={"rawData": base64.b64encode(xml.encode()).decode()}
        )
        session.get.return_value = FakeResponse(
            text="Стоматологическая клиника Урсула, Екатеринбург. Телефон +7 (343) 223-04-73",
            url="https://www.ursula.pro/",
        )
        lead = Lead(
            name="Урсула",
            city="Екатеринбург",
            phone="+7 343 223-04-73",
            website="http://www.ursula.ru",
            website_source="OpenStreetMap",
            verification_status="source_provided",
            audit=WebsiteAudit(state="broken", normalized_url="http://www.ursula.ru"),
        )

        result, stats = verify_missing_leads(
            [lead], "key", "folder", max_requests=1, session=session
        )

        self.assertEqual(result[0].website, "https://www.ursula.pro")
        self.assertEqual(result[0].website_source, "Yandex Search API")
        self.assertEqual(result[0].verification_status, "site_found")
        self.assertIsNone(result[0].audit)
        self.assertIn("http://www.ursula.ru", " ".join(result[0].verification_evidence))
        self.assertEqual(stats["sites_found"], 1)

    def test_working_osm_site_is_not_searched_automatically(self):
        session = Mock()
        lead = Lead(
            name="Компания",
            website="https://company.ru",
            website_source="OpenStreetMap",
            verification_status="source_provided",
            audit=WebsiteAudit(state="reachable", normalized_url="https://company.ru"),
        )

        result, stats = verify_missing_leads(
            [lead], "key", "folder", max_requests=1, session=session
        )

        self.assertEqual(result[0].website, "https://company.ru")
        self.assertEqual(stats["api_requests"], 0)
        session.post.assert_not_called()

    def test_broken_osm_site_keeps_old_url_when_no_replacement_is_found(self):
        xml = "<yandexsearch><response><results><grouping /></results></response></yandexsearch>"
        session = Mock()
        session.post.return_value = FakeResponse(
            payload={"rawData": base64.b64encode(xml.encode()).decode()}
        )
        audit = WebsiteAudit(state="broken", normalized_url="https://old.ru")
        lead = Lead(
            name="Компания",
            website="https://old.ru",
            website_source="OpenStreetMap",
            verification_status="source_provided",
            audit=audit,
        )

        result, stats = verify_missing_leads(
            [lead], "key", "folder", max_requests=1, session=session
        )

        self.assertEqual(result[0].website, "https://old.ru")
        self.assertEqual(result[0].verification_status, "source_provided")
        self.assertIs(result[0].audit, audit)
        self.assertEqual(stats["sites_found"], 0)

    def test_broken_osm_site_keeps_old_url_on_api_error(self):
        session = Mock()
        session.post.side_effect = requests.Timeout()
        audit = WebsiteAudit(state="broken", normalized_url="https://old.ru")
        lead = Lead(
            name="Компания",
            website="https://old.ru",
            website_source="OpenStreetMap",
            verification_status="source_provided",
            audit=audit,
        )

        result, stats = verify_missing_leads(
            [lead], "key", "folder", max_requests=1, session=session
        )

        self.assertEqual(result[0].website, "https://old.ru")
        self.assertEqual(result[0].verification_status, "source_provided")
        self.assertIs(result[0].audit, audit)
        self.assertEqual(stats["api_requests"], 1)

    def test_same_domain_is_not_counted_as_replacement(self):
        xml = "<yandexsearch><response><results><grouping><group><doc><url>https://old.ru/</url></doc></group></grouping></results></response></yandexsearch>"
        session = Mock()
        session.post.return_value = FakeResponse(
            payload={"rawData": base64.b64encode(xml.encode()).decode()}
        )
        session.get.return_value = FakeResponse(
            text="Компания в Екатеринбурге", url="https://old.ru/"
        )
        lead = Lead(
            name="Компания",
            city="Екатеринбург",
            website="http://old.ru",
            website_source="OpenStreetMap",
            verification_status="source_provided",
            audit=WebsiteAudit(state="broken", normalized_url="http://old.ru"),
        )

        result, stats = verify_missing_leads(
            [lead], "key", "folder", max_requests=1, session=session
        )

        self.assertEqual(result[0].website, "http://old.ru")
        self.assertEqual(result[0].verification_status, "source_provided")
        self.assertEqual(stats["sites_found"], 0)

    def test_crawl_extracts_contacts_from_same_domain_contact_page(self):
        session = Mock()
        session.get.side_effect = [
            FakeResponse(status_code=404, url="https://company.ru/robots.txt"),
            FakeResponse(
                text='<a href="/contacts">Контакты</a><form action="/send"></form>',
                url="https://company.ru/",
            ),
            FakeResponse(
                text='<a href="tel:+73432221100">Позвонить</a><a href="mailto:hello@company.ru">Email</a><a href="https://vk.com/company">VK</a><a href="https://evil.ru/contacts">Чужой сайт</a>',
                url="https://company.ru/contacts",
            ),
        ]

        result = crawl_contacts("https://company.ru", session=session)

        self.assertEqual(result["phone"], "+73432221100")
        self.assertEqual(result["email"], "hello@company.ru")
        self.assertEqual(result["social"], "https://vk.com/company")
        self.assertTrue(result["has_form"])
        self.assertEqual(result["contact_page"], "https://company.ru/contacts")
        self.assertEqual(session.get.call_count, 3)


if __name__ == "__main__":
    unittest.main()
