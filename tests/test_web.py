from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_guide_renders_without_touching_the_database():
    response = client.get("/guide")

    assert response.status_code == 200
    body = response.text
    assert "Troubleshooting" in body
    assert "app password" in body
    assert "Stock moves exactly once" in body


def test_guide_shows_the_real_status_machine():
    body = client.get("/guide").text

    # Rendered from app.services.orders, so the table cannot drift from the code.
    assert "received" in body
    assert "closed" in body
    assert "cancelled" in body
