from otto import config
from otto import settings as settings_module


class TestGetConfig:
    def test_returns_the_same_instance_on_repeat_calls(self):
        # Given a fresh configuration cache
        config.get_config.cache_clear()

        # When the configuration is requested twice
        first = config.get_config()
        second = config.get_config()

        # Then both callers share one wired instance
        assert first is second

    def test_wires_the_settings_singleton(self):
        # Given a fresh configuration cache
        config.get_config.cache_clear()

        # When the configuration is built
        built = config.get_config()

        # Then it carries the module-level settings singleton
        assert built.settings is settings_module.settings
