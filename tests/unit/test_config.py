from otto import config
from otto import settings as settings_module
from otto.domain.support import changes


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


class TestLoadReviewer:
    def _config(self, tmp_path, **settings_overrides) -> config.Configuration:
        built = config.Configuration(
            settings=settings_module.Settings(_env_file=None, **settings_overrides)
        )
        built.repo_host = changes.LocalRepoHost(root=tmp_path)
        return built

    def test_wires_a_reviewer_on_the_named_model(self, tmp_path):
        # Given settings that name a reviewer model, and a repository host
        built = self._config(tmp_path, reviewer_model="any-other-model")

        # When the reviewer is loaded
        built.load_reviewer()

        # Then proposals get a second opinion from that model
        assert built.change_reviewer.model_name == "any-other-model"

    def test_wires_no_reviewer_without_a_model(self, tmp_path):
        # Given settings that name no reviewer model
        built = self._config(tmp_path)

        # When the reviewer is loaded
        built.load_reviewer()

        # Then approvers get the edits alone
        assert built.change_reviewer is None
