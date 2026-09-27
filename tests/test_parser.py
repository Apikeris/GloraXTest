import gzip
import json
from pathlib import Path

import pytest

from glorax.parser import (SourceError, RobotsRules, collect_catalog, collect_offer_range,
                           extract_flight, normalize_project, parse_catalog, parse_detail)

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(name):
    return gzip.decompress((FIXTURES / name).read_bytes()).decode()


def flight_html(payload):
    return '<script>self.__next_f.push(' + json.dumps([1, '1:' + json.dumps(payload, ensure_ascii=False) + '\n']) + ')</script>'


def test_saved_catalog_reads_all_regions_even_cards_after_show_more():
    catalog = parse_catalog(fixture('catalog.html.gz'))
    assert catalog['complete']
    assert len(catalog['rows']) == catalog['expected']
    assert len(catalog['rows']) > 9  # saved HTML initially shows only nine
    assert len({row['cityName'] for row in catalog['rows']}) >= 8
    assert any('Скоро в продаже' in str(row['tags']) for row in catalog['rows'])
    assert any('Дом сдан' in str(row['tags']) for row in catalog['rows'])


def test_detail_project_identity_and_foreign_menu_exclusion():
    html = fixture('moskovsky.html.gz')
    detail = parse_detail(html, 'glorax-urickogo')
    assert detail['projectName'] == 'Московский'
    assert detail['cityName'] == 'Казань'
    with pytest.raises(SourceError, match='logs.slug'):
        parse_detail(html, 'glorax-ekocity')  # exists in menu and recommendations
    catalog = parse_catalog(fixture('catalog.html.gz'))
    row = next(row for row in catalog['rows'] if row['projectSlug'] == 'glorax-urickogo')
    project = normalize_project(row, detail, '2026-09-27T01:00:00+00:00')
    values = {f['key']: f['value'] for f in project['facts'] if f['verification_status'] == 'verified'}
    assert values['city'] == 'Казань'
    assert values['building_count'] == '3 корпуса'
    assert values['floor_range'] == '8-20 этажа'
    assert any(f['category'] == 'architecture' and f['verification_status'] == 'needs_review' for f in project['facts'])
    assert any(f['key'] == 'completion_date' and f['scope']['level'] == 'building' for f in project['facts'])
    assert project['coverage']['documents_for_review']
    assert any(c['field'] == 'transport_minutes' for c in project['coverage']['source_conflicts'])
    assert all(f['verification_status'] == 'needs_review' for f in project['facts'] if f['key'] == 'nearest_transport_minutes')


def test_null_values_never_replaced_with_zero_or_other_projects():
    row = next(row for row in parse_catalog(fixture('catalog.html.gz'))['rows'] if row['projectSlug'] == 'glorax-petergof')
    project = normalize_project(row)
    facts = {f['key']: f for f in project['facts']}
    assert facts['advertised_min_price']['value'] is None
    assert facts['advertised_min_price']['missing_reason']
    assert facts['max_price']['value'] is None
    assert facts['region']['value'] is None
    assert not any(f['value'] == 0 for f in project['facts'])


def test_marketing_minimum_is_not_maximum_or_complete_range():
    row = parse_catalog(fixture('catalog.html.gz'))['rows'][0]
    project = normalize_project(row)
    facts = {f['key']: f for f in project['facts'] if not f['scope'].get('rooms')}
    minimum = facts['advertised_min_price']
    assert isinstance(minimum['value'], str)
    assert minimum['conditions']['basis'] == 'advertised_minimum'
    assert not minimum['conditions']['sample_complete']
    assert minimum['verification_status'] == 'needs_review'
    assert facts['max_price']['value'] is None


def test_room_type_prices_and_project_metrics_create_scoped_facts():
    row = {"id": 8, "projectSlug": "sample", "projectName": "Проект", "cityName": "Регион",
           "tags": [{"label": "Скидка 10%"}], "hidePriceFlg": False,
           "flatType": [{"type": "0", "typeSlug": "flat", "price": 4_000_000, "square": 28.5},
                        {"type": "1", "typeSlug": "flat", "price": 4_500_000, "square": 31.2}]}
    detail = {"aboutProject": {
        "projectParams": [{"title": "5", "description": "Количество секций"},
                          {"title": "7–12 этажей", "description": "Этажность"}],
        "statistics": [{"title": "46,8 Га", "description": "площадь участка"},
                       {"title": "4", "description": "очереди строительства"},
                       {"title": "720", "description": "мест в 2 детских садах"},
                       {"title": "1150", "description": "мест в школе"}]}}
    facts = normalize_project(row, detail, '2026-09-27T01:00:00+00:00')['facts']
    room_prices = [f for f in facts if f['key'] == 'advertised_min_price' and f['scope'].get('rooms')]
    assert [(f['scope']['rooms'], f['value']) for f in room_prices] == [('0', '4000000'), ('1', '4500000')]
    assert all(f['verification_status'] == 'verified' and f['conditions']['basis'] == 'advertised_minimum' for f in room_prices)
    assert all(f['conditions']['price_basis'] == 'total' and f['conditions']['sample_complete'] is False for f in room_prices)
    metrics = {f['key']: f for f in facts if f['key'] in {'land_area', 'construction_phase_count', 'kindergarten_places', 'school_places', 'section_count'}}
    assert metrics['land_area']['value'] == '46.8' and metrics['land_area']['unit'] == 'га'
    assert metrics['construction_phase_count']['value'] == '4'
    assert metrics['kindergarten_places']['value'] == '720'
    assert metrics['school_places']['value'] == '1150'
    assert metrics['section_count']['value'] == '5'


def test_pdf_text_extraction_is_bounded_and_page_attributed():
    from io import BytesIO
    from pypdf import PdfWriter
    from glorax.parser import extract_pdf_text
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    output = BytesIO()
    writer.write(output)
    content = output.getvalue()
    assert extract_pdf_text(content) == []  # image/scanned PDFs are not guessed or OCR'd
    with pytest.raises(SourceError, match='не PDF'):
        extract_pdf_text(b'<html>Not a PDF</html>')


def test_pdf_collector_only_reads_robot_allowed_linked_assets():
    from glorax.parser import HTTPClient
    import requests
    client = HTTPClient.__new__(HTTPClient)
    client.document_rules = RobotsRules('User-agent: *\nDisallow: /assets/')
    client.document_robots_checked = True
    client.document_unavailable = False
    client.document_downloaded_bytes = 0
    client.retries = 1
    client.session = requests.Session()
    with pytest.raises(SourceError, match='безопасной ссылкой'):
        client.get_booklet_pdf('https://attacker.example/assets/00000000-0000-0000-0000-000000000000.pdf')
    with pytest.raises(SourceError, match='robots'):
        client.get_booklet_pdf('https://cms-dev.city-digital.ru/assets/00000000-0000-0000-0000-000000000000.pdf')


def test_discovered_pagination_followed_without_guessing_urls():
    rows = parse_catalog(fixture('catalog.html.gz'))['rows'][:2]
    pages = {
        'https://glorax.com/projects': flight_html({'data': rows[:1], 'projectsCnt': 2, 'nextPageUrl': '/projects?page=2'}),
        'https://glorax.com/projects?page=2': flight_html({'data': rows[1:], 'projectsCnt': 2})}
    class Client:
        base_url = 'https://glorax.com'
        visited = []
        def get(self, url):
            self.visited.append(url)
            return pages[url]
    client = Client()
    result, complete, coverage, _ = collect_catalog(client)
    assert complete and len(result) == 2
    assert coverage['pages'] == 2
    assert client.visited == list(pages)


def test_incomplete_catalog_is_reported_and_structure_change_fails():
    row = parse_catalog(fixture('catalog.html.gz'))['rows'][0]
    class Client:
        base_url = 'https://glorax.com'
        def get(self, url):
            return flight_html({'data': [row], 'projectsCnt': 2})
    _, complete, coverage, _ = collect_catalog(Client())
    assert not complete and coverage['found'] == 1
    with pytest.raises(SourceError, match='структура'):
        parse_catalog('<html><h1>Новый дизайн</h1></html>')


def test_robots_wildcards_not_bypassed():
    rules = RobotsRules('User-agent: *\nDisallow: *?project*\nDisallow: /*?city=*\nDisallow: /projects/*/genplan\nAllow: /*.js\n')
    assert rules.allowed('https://glorax.com/projects')
    assert rules.allowed('https://glorax.com/projects/glorax-urickogo')
    assert not rules.allowed('https://glorax.com/flats?project=glorax-urickogo')
    assert not rules.allowed('https://glorax.com/projects?city=kazan')
    assert not rules.allowed('https://glorax.com/projects/glorax-urickogo/genplan')


def test_flight_text_lengths_are_utf8_bytes_and_js_is_never_executed():
    text = 'Пример\nданных'
    transport = 'a:T' + format(len(text.encode()), 'x') + ',' + text + 'b:' + json.dumps({'text': '$a'}) + '\n'
    html = '<script>self.__next_f.push(' + json.dumps([1, transport]) + ');throw new Error("must not run")</script>'
    assert {'text': text} in extract_flight(html)


def offer(ident, price, **kw):
    return {'id': ident, 'price': price, 'property_type': 'flat', 'currency': 'RUB',
            'payment_terms': 'full_payment', 'price_basis': 'total', **kw}


def test_ranges_use_decimal_and_homogeneous_complete_sample():
    result = collect_offer_range([offer('1', '9999999.99'), offer('2', '10000000.01')],
        property_type='flat', payment_terms='full_payment', complete=True, expected_count=2,
        started_at='2026-09-27T01:00:00Z', finished_at='2026-09-27T01:00:02Z')
    assert result['minimum'] == '9999999.99' and result['maximum'] == '10000000.01'
    assert result['complete'] and result['count'] == 2
    assert result['started_at'] != result['finished_at']


def test_ranges_do_not_mix_property_types_payment_promos_or_per_metre():
    offers = [offer('1', '9000000'), offer('2', '700000', property_type='storage'),
              offer('3', '100000', price_basis='per_sqm'), offer('4', '5000000', payment_terms='promotion'),
              offer('5', '1000', currency='USD'), offer('1', '9000000')]
    result = collect_offer_range(offers, property_type='flat', payment_terms='full_payment', complete=True, expected_count=6)
    assert result['minimum'] == result['maximum'] == '9000000'
    assert result['count'] == 1 and not result['complete']
    assert result['basis'] == 'found_offers_only'
    assert len(result['rejected_ids']) == 5
    empty = collect_offer_range([], property_type='flat')
    assert empty['minimum'] is None and empty['maximum'] is None and empty['missing_reason']


def test_premium_landing_identity_does_not_trust_global_h1():
    from glorax.parser import parse_landing
    payload={'logs':{'projectFlatsUrl':'/flats?project=correct-slug','heroScreen':{'address':'Подтверждённый адрес'}},'component':{'projectSlug':'correct-slug','citySlug':'city'}}
    html='<link rel="canonical" href="https://glorax.com/correct-slug"><h1>Чужой проект из меню</h1>'+flight_html(payload)
    detail=parse_landing(html,'correct-slug','https://glorax.com/correct-slug')
    assert detail['_landing_logs']['heroScreen']['address']=='Подтверждённый адрес'
    with pytest.raises(SourceError):parse_landing(html,'wrong-slug','https://glorax.com/correct-slug')


def test_http_redirect_checks_host_and_robots(monkeypatch):
    from glorax.parser import HTTPClient
    import requests
    calls=[]
    class Response:
        status_code=308
        headers={'Location':'https://attacker.example/stolen'}
        def close(self):pass
    def get(*a,**kw):calls.append(a);return Response()
    client=HTTPClient.__new__(HTTPClient);client.base_url='https://glorax.com';client.delay=0;client.retries=0;client.last_request=0;client.rules=RobotsRules('User-agent: *\nDisallow: /forbidden');client.final_urls={};client.session=requests.Session()
    monkeypatch.setattr(client.session,'get',get)
    with pytest.raises(SourceError,match='пределы'):client.get('https://glorax.com/projects')
    assert len(calls)==1
    Response.headers={'Location':'/forbidden'}
    with pytest.raises(SourceError,match='robots'):client.get('https://glorax.com/projects')
