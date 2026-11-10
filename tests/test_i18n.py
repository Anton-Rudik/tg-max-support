from app.config import Settings
from app.platforms import client_text, tg_menu, max_menu


def test_localized_client_menus():
    assert tg_menu("en")[0][0]["text"] == "📚 Resources"
    assert max_menu("en")[1][0]["text"] == "💬 Contact support"
    assert client_text("en", "welcome", project_name="Example Inc") == (
        "Welcome to Example Inc! Feel free to message us here if you have any questions."
    )
    assert tg_menu("nonexistent") == tg_menu("ru")


def test_brand_configuration():
    s = Settings(_env_file=None, project_name="Example Inc", ui_language="en")
    assert s.project_name == "Example Inc"
