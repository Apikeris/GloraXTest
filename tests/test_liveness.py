from glorax import create_app


def test_render_liveness_routes_do_not_require_database():
    app = create_app({'TESTING': True})
    client = app.test_client()

    health = client.get('/healthz')
    assert health.status_code == 200
    assert health.get_json() == {'status': 'ok'}

    # Render's port scanner sends HEAD /. The catalogue GET performs database
    # queries, but the port check must remain available during a DB outage.
    head = client.head('/')
    assert head.status_code == 200
    assert head.data == b''
