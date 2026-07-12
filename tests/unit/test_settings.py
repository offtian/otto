from otto import settings as settings_module


class TestSettings:
    def test_applies_defaults_when_no_env_vars_are_set(self, monkeypatch):
        # Given an environment with no overrides
        monkeypatch.delenv("DEBUG", raising=False)
        monkeypatch.delenv("LOG_LEVEL", raising=False)

        # When a Settings instance is created without an env file
        settings = settings_module.Settings(_env_file=None)

        # Then the documented defaults apply
        assert settings.debug is False
        assert settings.log_level == "INFO"

    def test_reads_values_from_the_environment(self, monkeypatch):
        # Given an environment that overrides the log level
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")

        # When a Settings instance is created without an env file
        settings = settings_module.Settings(_env_file=None)

        # Then the environment value wins over the default
        assert settings.log_level == "DEBUG"
