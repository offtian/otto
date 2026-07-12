import pathlib

from otto import settings as settings_module


class TestEnvExampleParity:
    def test_every_settings_field_appears_in_env_example(self):
        # Given the keys documented in .env.example
        env_example = pathlib.Path(__file__).parents[2] / ".env.example"
        documented = {
            line.split("=", 1)[0].strip().lower()
            for line in env_example.read_text().splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        }

        # When compared with the fields Settings actually reads
        fields = set(settings_module.Settings.model_fields)

        # Then no Settings field is missing from the example file
        # (compose-only extras in the file are fine — subset, not equality)
        assert fields <= documented
