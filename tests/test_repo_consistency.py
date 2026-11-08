"""Repository conventions from AGENTS.md, enforced as tests (standard library only)."""
import ast
import os
import re
import sys
import unittest

from support import ADDON, APP, ROOT


def read(*parts):
    with open(os.path.join(ADDON, *parts), encoding="utf-8") as f:
        return f.read()


def section_keys(text, section):
    """Keys indented by two spaces directly under the top-level `section:` of a YAML file."""
    keys, inside = [], False
    for line in text.splitlines():
        if re.match(rf"^{section}:\s*$", line):
            inside = True
        elif inside and line.strip() and not line.startswith((" ", "#")):
            break
        elif inside and (m := re.match(r"^  ([A-Za-z0-9_]+):", line)):
            keys.append(m.group(1))
    return keys


def section_list(text, section):
    """Items of the `- value` list directly under the top-level `section:`."""
    items, inside = [], False
    for line in text.splitlines():
        if re.match(rf"^{section}:\s*$", line):
            inside = True
        elif inside and (m := re.match(r"^  - (\S+)\s*$", line)):
            items.append(m.group(1))
        elif inside and line.strip() and not line.startswith("#"):
            break
    return items


def translation_fields(text):
    """{option: {field: value}} from translations/<lang>.yaml (`configuration:` section)."""
    out, option, inside = {}, None, False
    for line in text.splitlines():
        if re.match(r"^configuration:\s*$", line):
            inside = True
        elif inside and line.strip() and not line.startswith((" ", "#")):
            break
        elif inside and (m := re.match(r"^  ([A-Za-z0-9_]+):\s*$", line)):
            option = m.group(1)
            out[option] = {}
        elif inside and option and (m := re.match(r"^    ([a-z]+):\s*(.*\S)\s*$", line)):
            out[option][m.group(1)] = m.group(2).strip("\"'")
    return out


def version_tuple(text):
    return tuple(int(n) for n in text.split("."))


class AddonConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = read("config.yaml")
        cls.options = section_keys(cls.config, "options")

    def test_options_and_schema_declare_the_same_keys(self):
        self.assertTrue(self.options)
        self.assertEqual(sorted(self.options), sorted(section_keys(self.config, "schema")))

    def test_every_option_is_translated_in_english_and_french(self):
        for lang in ("en", "fr"):
            fields = translation_fields(read("translations", f"{lang}.yaml"))
            with self.subTest(lang=lang):
                self.assertEqual(sorted(fields), sorted(self.options))
                for option, values in fields.items():
                    self.assertTrue(values.get("name"), f"{lang}: {option} has no name")
                    self.assertTrue(values.get("description"), f"{lang}: {option} has no description")

    def test_every_option_is_documented_in_both_languages(self):
        for doc in ("DOCS.md", "DOCS.fr.md"):
            text = read(doc)
            for option in self.options:
                with self.subTest(doc=doc, option=option):
                    self.assertIn(f"`{option}`", text)

    def test_architectures_have_a_base_image(self):
        arch = section_list(self.config, "arch")
        self.assertTrue(arch)
        self.assertEqual(sorted(arch), sorted(section_keys(read("build.yaml"), "build_from")))

    def test_mqtt_service_is_required(self):
        self.assertRegex(self.config, r"(?m)^  - mqtt:need\s*$")


class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.run_sh = read("run.sh")
        cls.options = section_keys(read("config.yaml"), "options")
        cls.python = "\n".join(read("app", name) for name in ("hypon_bridge.py", "hypon_intercept.py"))

    def test_run_sh_only_reads_declared_options(self):
        used = set(re.findall(r"bashio::config(?:\.\w+)? '(\w+)'", self.run_sh))
        self.assertEqual(used - set(self.options), set())

    def test_every_option_is_wired_in_run_sh(self):
        used = set(re.findall(r"bashio::config(?:\.\w+)? '(\w+)'", self.run_sh))
        self.assertEqual(set(self.options) - used, set())

    def test_every_exported_variable_is_read_by_the_python_code(self):
        for name in sorted(set(re.findall(r"export (\w+)=", self.run_sh))):
            with self.subTest(variable=name):
                self.assertIn(f'"{name}"', self.python)


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.version = re.search(r'(?m)^version:\s*"?(\d+\.\d+\.\d+)"?\s*$', read("config.yaml")).group(1)
        cls.changelog = read("CHANGELOG.md")
        cls.entries = re.findall(r"(?m)^##\s+\[?(\d+\.\d+\.\d+)\]?", cls.changelog)

    def test_changelog_top_entry_matches_the_add_on_version(self):
        self.assertTrue(self.entries)
        self.assertEqual(self.entries[0], self.version)

    def test_changelog_is_newest_first_without_duplicates(self):
        ordered = sorted(self.entries, key=version_tuple, reverse=True)
        self.assertEqual(self.entries, ordered)
        self.assertEqual(len(self.entries), len(set(self.entries)))

    @unittest.skipIf(sys.platform == "win32", "symlinks are not reliable on Windows checkouts")
    def test_root_changelog_is_a_symlink_to_the_add_on_changelog(self):
        root = os.path.join(ROOT, "CHANGELOG.md")
        self.assertTrue(os.path.islink(root))
        self.assertEqual(os.path.realpath(root), os.path.realpath(os.path.join(ADDON, "CHANGELOG.md")))


class SourceTests(unittest.TestCase):
    def test_python_uses_the_standard_library_only(self):
        local = {"hypon_bridge", "hypon_intercept"}
        for name in sorted(os.listdir(APP)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(APP, name), encoding="utf-8") as f:
                tree = ast.parse(f.read(), name)
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    modules = [node.module]
                for module in modules:
                    top = module.split(".")[0]
                    with self.subTest(file=name, module=module):
                        self.assertTrue(top in sys.stdlib_module_names or top in local,
                                        f"{name} imports third-party module {module!r}")


if __name__ == "__main__":
    unittest.main()
