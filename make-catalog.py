import argparse
from collections import namedtuple
from functools import cached_property
from io import StringIO
import itertools
import logging
import os
import re
import semver
import shutil
import subprocess
import sys
import yaml


class CataloggerCommandLineArgs:
    debug = False

    def __init__ (self):
        parser = argparse.ArgumentParser(
                    prog='make-catalog.py',
                    description='Process `schema: olm.*` YAML records into catalogs')

        parser.add_argument('--configs-out')
        parser.add_argument('--cache-out')
        parser.add_argument('--debug', action='store_true')
        parser.add_argument('input',  nargs='*')

        args = parser.parse_args()
        self.configs_out = args.configs_out
        self.cache_out = args.cache_out
        self.inputs = args.input
        self.debug = args.debug


class Catalogger:
    def __init__ (self, args):
        self.args = args
        self.logger = CataloggerLogger()
        self.logger.setLevel(logging.DEBUG if self.args.debug else logging.INFO)

    def render (self):
        with open(os.path.join(self.configs_out, "index.yaml"), "w") as configs_out_fd:
            def print_yaml (yaml_string):
                configs_out_fd.write(yaml_string)
                configs_out_fd.write("\n---\n")

            for input_filename in self.args.inputs:
                package = OlmPackageParser(self.logger, input_filename)
                print_yaml(package.olm_package_yaml)
                for channel in package.channels:
                    print_yaml(channel.olm_channel_yaml)

            for bundle_version in BundleVersion.all_loaded():
                for y in bundle_version.yamls:
                    print_yaml(y)

    @property
    def has_opm (self):
        return shutil.which("opm") is not None

    def validate (self):
        return run_opm(["validate", self.configs_out])

    def cacheify (self):
        return run_opm(["serve", "--cache-only",
                              self.configs_out,
                              f"--cache-dir={self.args.cache_out}"])

    @property
    def configs_out (self):
        configs_out = self.args.configs_out
        self._ensure_dir(configs_out)
        return configs_out


    @property
    def cache_out (self):
        cache_out = self.args.cache_out
        self._ensure_dir(cache_out)
        return cache_out

    def _ensure_dir (self, dir):
        try:
            os.makedirs(dir)
        except FileExistsError:
            pass


def run_opm (cmdline_args, *args, **kwargs):
    if "check" not in kwargs:
        kwargs["check"] = True

    cmdline = ["opm"] + cmdline_args

    if "logger" in kwargs:
        logger = kwargs.pop("logger")
        logger.info("Running " + " ".join(cmdline))

    return subprocess.run(cmdline, *args, **kwargs)

class CataloggerLogger:
    def __init__ (self):
        self.prefixes = []
        self.logger = logging.getLogger(__name__)
        self.logger.propagate = False

        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(self)
        self.logger.addHandler(ch)

    def temp_prefix (self, prefix):
        this = self
        class TempPrefixContext:
            def __enter__ (self):
                this.prefixes.append(prefix)

            def __exit__ (self, exn_type, exn_value, exn_traceback):
                this.prefixes.pop()

        return TempPrefixContext()

    def setLevel (self, *args, **kwargs):
        """Delegated to `self.logger`."""
        self.logger.setLevel(*args, **kwargs)

    def debug (self, msg, *args, **kwargs):
        """Delegated to `self.logger`."""
        self.logger.debug(msg, *args, **kwargs)

    def info (self, msg, *args, **kwargs):
        """Delegated to `self.logger`."""
        self.logger.info(msg, *args, **kwargs)

    def warning (self, msg, *args, **kwargs):
        """Delegated to `self.logger`."""
        self.logger.warning(msg, *args, **kwargs)

    def fatal (self, msg, *args, **kwargs):
        """Delegated to `self.logger`."""
        self.logger.fatal(msg, *args, **kwargs)

    @property
    def _log_prefix (self):
        return "". join(f"{p}: " for p in self.prefixes)

    def format(self, record):
        grey = "\x1b[38;20m"
        yellow = "\x1b[33;20m"
        red = "\x1b[31;20m"
        bold_red = "\x1b[31;1m"
        reset = "\x1b[0m"
        COLORS = {
            logging.DEBUG: grey,
            logging.INFO: grey,
            logging.WARNING: yellow,
            logging.ERROR: red,
            logging.CRITICAL: bold_red
        }
        log_fmt = f"{ COLORS.get(record.levelno) }%(levelname)s:{ reset } %(message)s"
        formatter = logging.Formatter(log_fmt)

        record.msg = f"{ self._log_prefix }{ record.msg }"
        return formatter.format(record)


class OlmPackageParser:
    def __init__ (self, logger, yaml_filename):
        self.logger = logger
        self.yaml_filename = yaml_filename

        with open(self.yaml_filename) as yaml_fd:
            with self.logger.temp_prefix(self.yaml_filename):
                self._yaml_docs = list(split_yaml_documents(yaml_fd))

    @property
    def olm_package_yaml (self):
        for _, yaml_doc in self._yaml_docs:
            if 'schema: olm.package' in yaml_doc:
                return yaml_doc

    @property
    def channels (self):
        for lineno, yaml_doc in self._yaml_docs:
            if 'schema: olm.channel' in yaml_doc:
                with self.logger.temp_prefix(f'{self.yaml_filename}: YAML document starting at line {lineno}'):
                    yield OlmChannelParser(self.logger, yaml_doc)


class OlmChannelParser:
    def __init__ (self, logger, yaml_channel_string):
        self.logger = logger
        self._yaml_unexpanded = yaml_channel_string
        self._load_entries()

    @cached_property
    def _parsed (self):
        return re.match(
            "(.*)^_versions:(.*?)(^[a-zA-Z].*)?\\Z", self._yaml_unexpanded,
            re.MULTILINE|re.DOTALL)

    @property
    def yaml_prologue_string (self):
        return (self._parsed[1] if self._parsed is not None else "")

    @property
    def image_versions (self):
        return (yaml.safe_load(self._parsed[2])
                if self._parsed is not None and self._parsed[2] is not None
                else None)

    @property
    def yaml_epilogue_string (self):
        return (self._parsed[3]
                if self._parsed is not None and self._parsed[3] is not None
                else "")

    def _load_entries (self):
        versions = self.image_versions
        if not versions:
            self.logger.warning("Found channel without an expansion section")
            self.olm_channel_yaml = self._yaml_unexpanded

        entries = []
        for version in versions:

            for b in BundleVersion.enumerate(
                    logger=self.logger,
                    versions_info=version):
                for y in b.yamls:
                    y = yaml.safe_load(y)
                    if y["schema"] == "olm.bundle":
                        entries.append(dict(
                            name=y["name"]
                        ))
                    if len(entries) > 1:
                        entries[-1]["replaces"] = entries[-2]["name"]

        self.olm_channel_yaml = f"""
{ self.yaml_prologue_string }
entries:
{ yaml.safe_dump(entries) }
{ self.yaml_epilogue_string }
"""


class ImageVersion:
    def __init__ (self, prefix, ver):
        self.prefix = prefix
        self.ver = (ver if isinstance(ver, semver.Version)
                    else semver.parse_version_info(ver))

    @classmethod
    def parse  (cls, version_string):
        matched = re.match('(v?)([0-9.]*)$', version_string)
        if not matched:
            raise ValueError("Unable to parse f{version_string}")

        return cls(prefix=matched[1], ver=matched[2])

    def inc_patchlevel (self):
        that = self.__class__(prefix=self.prefix,
                              ver=self.ver.replace(patch=self.ver.patch + 1))
        return that

    def __repr__ (self):
        return f"{self.prefix}{self.ver}"

    def __eq__ (self, other):
        return (self.prefix == other.prefix) and self.ver.__eq__(other.ver)

    def __gt__ (self, other):
        return self.ver.__gt__(other.ver)

    def __ge__ (self, other):
        return self.ver.__ge__(other.ver)

    def __lt__ (self, other):
        return self.ver.__lt__(other.ver)

    def __le__ (self, other):
        return self.ver.__le__(other.ver)


class BundleVersion:
    def __init__ (self, version, yamls):
        self.version = version
        self.yamls = yamls

    _load_cache = {}

    @classmethod
    def load (cls, logger, docker_image_name, expected_version):
        if docker_image_name not in cls._load_cache:
            cls._load_cache[docker_image_name] = cls._do_load(
                logger, docker_image_name, expected_version)
        return cls._load_cache[docker_image_name]

    @classmethod
    def all_loaded (cls):
        return (bv for bv in cls._load_cache.values() if bv is not None)

    @classmethod
    def _do_load (cls, logger, docker_image_name, expected_version):
        try:
            opm_rendered = run_opm(
                ["render", docker_image_name, "--output=yaml"],
                logger=logger, capture_output=True)
        except subprocess.CalledProcessError:
            return None

        yamls = list(r[1] for r in split_yaml_documents(opm_rendered.stdout))
        for y in yamls:
            for prop in yaml.safe_load(y)["properties"]:
                if prop["type"] == "olm.package":
                    actual_version = prop["value"]["version"]
                    if actual_version != expected_version.ver:
                        logger.warning(f"Skipping malformed image f{docker_image_name} (contains version {actual_version}, expected {expected_version.ver})")
                        return None
                    else:
                        return cls(version=expected_version,
                                   yamls=yamls)

        failure = f"No `olm.package` property found in {docker_image_name}!"
        logger.warning(failure)
        return None

    @classmethod
    def enumerate (cls, logger, versions_info):
        """Yields all versions that exist, and their content, starting at `first_version`.

        :param logger: Something with `.debug(...)`, .info(...)`, `.warning(...)` etc. methods
        :param versions_info: A structure (dict) from the configuration file, describing the way to
                        enumerate versions.
        :param versions_info["pattern"]: The pattern of image names, as a string, with the `@@VERSION@@` inside
                        to be replaced with semver-style versions.
        :param versions_info["from"]: The first value to substitute `@@VERSION@@` with in `pattern`
        :type first_version: ImageVersion
        :param versions_info["failures"]: The failure budget. `enumerate_bundle_versions` starts at `first_version`,
                         incrementing the patchlevel one by one, until `opm render` has failed
                         one more time than `failures`; it then stops.

        :rtype: Iterator[:class:`BundleVersion`]
        """

        pattern = versions_info["pattern"]
        first_version = ImageVersion.parse(versions_info["from"])
        failures = versions_info.get("failures", 1)
        if 'to' in versions_info:
            last_version = ImageVersion.parse(versions_info['to'])
        else:
            last_version = None   # Stop only when we can't find any

        to_skip = set(versions_info.get("skip", []))

        current_version = first_version
        successes = 0
        while failures >= 0 and (last_version is None or current_version <= last_version):
            if str(current_version) not in to_skip:
                docker_image_name = re.sub('@@VERSION@@', str(current_version),
                                           pattern)
                bundle_version = cls.load(logger, docker_image_name, current_version)
                if bundle_version is None:
                    failures = failures - 1
                    bailing_out_maybe = ", bailing out" if failures < 0 else ""
                    logger.info(f"Could not load version {current_version}{bailing_out_maybe}")
                else:
                    successes = successes + 1
                    yield bundle_version

            current_version = current_version.inc_patchlevel()

        if not successes:
            msg = f"No single image could be found! pattern={pattern}, from={first_version}"
            logger.fatal(msg)
            raise ValueError(msg)


def split_yaml_documents (yaml_fd_or_string):
    """Split a YAML file into documents (separated by three dashes). Returns each as a string.
    Yields pairs of (starting line number, YAML text)."""

    if isinstance(yaml_fd_or_string, bytes):
        yaml_fd = (StringIO(yaml_fd_or_string.decode("utf-8")))
    elif isinstance(yaml_fd_or_string, str):
        yaml_fd = (StringIO(yaml_fd_or_string))
    else:
        yaml_fd = yaml_fd_or_string

    for key, group in itertools.groupby(
            enumerate(yaml_fd, start=1),
            lambda num_and_line: num_and_line[1].startswith('---')):
        if key:
            continue
        nums_and_lines = list(group)
        yaml_doc = "".join(num_and_line[1] for num_and_line in nums_and_lines)
        if re.search("\\S", yaml_doc):
            yield (nums_and_lines[0][0], yaml_doc)


if __name__ ==  '__main__':
    c = Catalogger(CataloggerCommandLineArgs())
    c.render()
    if c.has_opm:
        c.validate()
        c.cacheify()
