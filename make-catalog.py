import argparse
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
            for input_filename in self.args.inputs:
                with open(input_filename) as yaml_fd:
                    with self.logger.temp_prefix(input_filename):
                        for lineno, yaml_doc in split_yaml_documents(yaml_fd):
                            bundles = []
                            with self.logger.temp_prefix(
                                    f"YAML document starting at line {lineno}"):
                                if "schema: olm.channel" in yaml_doc:
                                    parser = OlmChannelParser(self.logger, yaml_doc)
                                    yaml_doc = parser.expand_entries()
                                    bundles.extend(list(parser.bundles))

                            configs_out_fd.write(yaml_doc)
                            configs_out_fd.write("\n---\n")
                            for olm_bundle in bundles:
                                configs_out_fd.write(olm_bundle)
                                configs_out_fd.write("\n---\n")

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


class OlmChannelParser:
    def __init__ (self, logger, yaml_channel_string):
        self.logger = logger
        self.yaml_channel_string = yaml_channel_string

    @cached_property
    def _parsed (self):
        return re.match(
            "(.*)^_versions:(.*?)(^[a-zA-Z].*)?\\Z", self.yaml_channel_string,
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

    def expand_entries (self):
        versions = self.image_versions
        if not versions:
            self.logger.warning("Found channel without an expansion section")
            return self.yaml_channel_string

        entries = []
        self.bundles = []
        for version in versions:
            current_version = BundleVersion.parse(version["from"])

            failures = getattr(version, 'failures', 1)
            while failures >= 0:
                docker_image_name = re.sub('@@VERSION@@', str(current_version),
                                           version['pattern'])
                try:
                    opm_rendered = run_opm(
                        ["render", docker_image_name, "--output=yaml"],
                        logger=self.logger, capture_output=True)
                except subprocess.CalledProcessError:
                    failures = failures - 1
                else:
                    rendered = list(r[1] for r in split_yaml_documents(opm_rendered.stdout))
                    self.bundles.extend(rendered)
                    for r in rendered:
                        r = yaml.safe_load(r)
                        if r["schema"] == "olm.bundle":
                            entries.append(dict(
                                name=r["name"]
                            ))
                            if len(entries) > 1:
                                entries[-1]["replaces"] = entries[-2]["name"]
                        break
            
                current_version = current_version.inc_patchlevel()

        if not entries:
            msg = f"No single image could be found! pattern={version["pattern"]}, from={version['from']}"
            self.logger.fatal(msg)
            raise ValueError(msg)

        return f"""
{ self.yaml_prologue_string }
entries:
{ yaml.safe_dump(entries) }
{ self.yaml_epilogue_string }
"""


class BundleVersion:
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
