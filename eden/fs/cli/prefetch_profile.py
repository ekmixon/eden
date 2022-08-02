# Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

import argparse
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path
from typing import List, Optional, Set

from facebook.eden.ttypes import Glob, GlobParams, PredictiveFetch

from . import subcmd as subcmd_mod, tabulate
from .cmd_util import get_eden_instance, require_checkout
from .config import EdenCheckout, EdenInstance
from .subcmd import Subcmd
from .util import get_eden_cli_cmd, get_environment_suitable_for_subprocess


prefetch_profile_cmd = subcmd_mod.Decorator()

# consults the global kill switch to check if this user should prefetch their
# active prefetch profiles.
def should_prefetch_profiles(instance: EdenInstance) -> bool:
    return instance.get_config_bool("prefetch-profiles.prefetching-enabled", False)


# consults the global kill switch to check if this user should run a predictive
# profile prefetch
def should_prefetch_predictive_profiles(instance: EdenInstance) -> bool:
    return instance.get_config_bool(
        "prefetch-profiles.predictive-prefetching-enabled", False
    )


# consults the global kill switch to check if this user should prefetch the
# metadata for the specified prefetch profile(s).
def should_prefetch_metadata(instance: EdenInstance) -> bool:
    return instance.get_config_bool("prefetch-profiles.prefetch-metadata", False)


# find the profile inside the given checkout and return a set of its contents.
def get_contents_for_profile(
    checkout: EdenCheckout, profile: str, silent: bool
) -> Set[str]:
    k_relative_profiles_location = "xplat/scm/prefetch_profiles/profiles"

    profile_path = checkout.path / k_relative_profiles_location / profile

    if not profile_path.is_file():
        if not silent:
            print(f"Profile {profile} not found for checkout {checkout.path}.")
        return set()

    with open(profile_path) as f:
        return {pat.strip() for pat in f.readlines()}


# Function to actually cause the prefetch, can be called on a background process
# or in the main process.
# Only print here if silent is False, as that could send messages randomly to
# stdout.
def make_prefetch_request(
    checkout: EdenCheckout,
    instance: EdenInstance,
    all_profile_contents: Set[str],
    enable_prefetch: bool,
    silent: bool,
    revisions: Optional[List[str]],
    predict_revisions: bool,
    prefetch_metadata: bool,
    background: bool,
    predictive: bool,
    predictive_num_dirs: int,
) -> Optional[Glob]:
    if predict_revisions:
        # The arc and hg commands need to be run in the mount mount, so we need
        # to change the working path if it is not within the mount.
        current_path = Path.cwd()
        in_checkout = False
        try:
            # this will throw if current_path is not a relative path of the
            # checkout path
            checkout.get_relative_path(current_path)
            in_checkout = True
        except Exception:
            os.chdir(checkout.path)

        bookmark_to_prefetch_command = ["arc", "stable", "best", "--verbose", "error"]
        bookmarks_result = subprocess.run(
            bookmark_to_prefetch_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=get_environment_suitable_for_subprocess(),
        )

        if bookmarks_result.returncode:
            raise Exception(
                "Unable to predict commits to prefetch, error finding bookmark"
                f" to prefetch: {bookmarks_result.stderr}"
            )

        bookmark_to_prefetch = bookmarks_result.stdout.decode().strip("\n")

        commit_from_bookmark_commmand = [
            "hg",
            "log",
            "-r",
            bookmark_to_prefetch,
            "-T",
            "{node}",
        ]
        commits_result = subprocess.run(
            commit_from_bookmark_commmand,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=get_environment_suitable_for_subprocess(),
        )

        if commits_result.returncode:
            raise Exception(
                "Unable to predict commits to prefetch, error converting"
                f" bookmark to commit: {commits_result.stderr}"
            )

        # if we changed the working path lets change it back to what it was
        # before
        if not in_checkout:
            os.chdir(current_path)

        raw_commits = commits_result.stdout.decode()
        # arc stable only gives us one commit, so for now this is a single
        # commit, but we might use multiple in the future.
        revisions = [re.sub("\n$", "", raw_commits)]

        if not silent:
            print(f"Prefetching for revisions: {revisions}")

    byte_revisions = None
    if revisions is not None:
        byte_revisions = [bytes.fromhex(revision) for revision in revisions]

    with instance.get_thrift_client_legacy() as client:
        if not predictive:
            return client.globFiles(
                GlobParams(
                    mountPoint=bytes(checkout.path),
                    globs=list(all_profile_contents),
                    includeDotfiles=False,
                    prefetchFiles=enable_prefetch,
                    suppressFileList=silent,
                    revisions=byte_revisions,
                    prefetchMetadata=prefetch_metadata,
                    background=background,
                )
            )
        predictiveParams = PredictiveFetch()
        if predictive_num_dirs > 0:
            predictiveParams.numTopDirectories = predictive_num_dirs
        return client.predictiveGlobFiles(
            GlobParams(
                mountPoint=bytes(checkout.path),
                includeDotfiles=False,
                prefetchFiles=enable_prefetch,
                suppressFileList=silent,
                revisions=byte_revisions,
                prefetchMetadata=prefetch_metadata,
                background=background,
                predictiveGlob=predictiveParams,
            )
        )


# prefetch all of the files specified by a profile in the given checkout
def prefetch_profiles(
    checkout: EdenCheckout,
    instance: EdenInstance,
    profiles: List[str],
    background: bool,
    enable_prefetch: bool,
    silent: bool,
    revisions: Optional[List[str]],
    predict_revisions: bool,
    prefetch_metadata: bool,
    predictive: bool,
    predictive_num_dirs: int,
) -> Optional[Glob]:

    if predictive and not should_prefetch_predictive_profiles(instance):
        if not silent:
            print(
                "Skipping Predictive Prefetch Profiles fetch due to global kill switch. "
                "This means prefetch-profiles.predictive-prefetching-enabled is not set in "
                "the EdenFS configs."
            )
        return None
    if not should_prefetch_profiles(instance) and not predictive:
        if not silent:
            print(
                "Skipping Prefetch Profiles fetch due to global kill switch. "
                "This means prefetch-profiles.prefetching-enabled is not set in "
                "the EdenFS configs."
            )
        return None

    if should_prefetch_metadata(instance):
        if not silent and not prefetch_metadata:
            print(
                "Prefetching file metadata due to global eden config. "
                "This means prefetch-profiles.prefetch_metadata is set in the "
                "EdenFS configs."
            )
        prefetch_metadata = True

    all_profile_contents = set()

    if not predictive:
        for profile in profiles:
            all_profile_contents |= get_contents_for_profile(checkout, profile, silent)
    return make_prefetch_request(
        checkout=checkout,
        instance=instance,
        all_profile_contents=all_profile_contents,
        enable_prefetch=enable_prefetch,
        silent=silent,
        revisions=revisions,
        predict_revisions=predict_revisions,
        prefetch_metadata=prefetch_metadata,
        background=background,
        predictive=predictive,
        predictive_num_dirs=predictive_num_dirs,
    )


def print_prefetch_results(results, print_commits) -> None:
    print("\nFiles Prefetched: ")
    # Can just print names it's clear which commit they come from
    if not print_commits:
        columns = ["FileName"]
        data = [{"FileName": os.fsdecode(name)} for name in results.matchingFiles]
    else:
        columns = ["FileName", "Commit"]
        data = [
            {"FileName": os.fsdecode(name), "Commit": commit.hex()}
            for name, commit in zip(results.matchingFiles, results.originHashes)
        ]

    print(tabulate.tabulate(columns, data))


@prefetch_profile_cmd("record", "Start recording fetched file paths.")
class RecordProfileCmd(Subcmd):
    def run(self, args: argparse.Namespace) -> int:
        instance = get_eden_instance(args)
        with instance.get_thrift_client_legacy() as client:
            client.startRecordingBackingStoreFetch()
        return 0


@prefetch_profile_cmd(
    "finish",
    "Stop recording fetched file paths and save previously"
    " collected fetched file paths in the output prefetch profile",
)
class FinishProfileCmd(Subcmd):
    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--output-path",
            nargs="?",
            help="The output path to store the prefetch profile",
        )

    def run(self, args: argparse.Namespace) -> int:
        instance = get_eden_instance(args)
        with instance.get_thrift_client_legacy() as client:
            files = client.stopRecordingBackingStoreFetch()
            output_path = args.output_path or os.path.abspath("prefetch_profile.txt")
            with open(output_path, "w") as f:
                for path in sorted(files.fetchedFilePaths["HgQueuedBackingStore"]):
                    f.write(os.fsdecode(path))
                    f.write("\n")
        return 0


@prefetch_profile_cmd(
    "list", "List all of the currenly activated prefetch profiles for a checkout."
)
class ListProfileCmd(Subcmd):
    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--checkout",
            help="The checkout for which you want to see all the profiles this profile.",
            default=None,
        )

    def run(self, args: argparse.Namespace) -> int:
        checkout = args.checkout

        instance, checkout, _rel_path = require_checkout(args, checkout)

        profiles = sorted(checkout.get_config().active_prefetch_profiles)

        columns = ["Name"]
        data = [{"Name": name} for name in profiles]

        print(tabulate.tabulate(columns, data))

        return 0


def check_positive_int(value) -> int:
    err = f"Integer > 0 required (got {value})"
    try:
        int_value = int(value)
        if int_value <= 0:
            raise argparse.ArgumentTypeError(err)
    except Exception:
        raise argparse.ArgumentTypeError(err)
    return int_value


def add_common_args(
    parser: argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    parser.add_argument(
        "--verbose",
        help="Print extra info including warnings and the names of the "
        "matching files to fetch.",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "--checkout",
        help="The checkout for which you want to activate this profile.",
        default=None,
    )
    parser.add_argument(
        "--skip-prefetch",
        help="Do not prefetch profiles only find all the files that match "
        "them. This will still list the names of matching files when the "
        "verbose flag is also used, and will activate the profile when running "
        "`activate`.",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "--foreground",
        help="Run the prefetch in the main thread rather than in the"
        " background. Normally this command will return once the prefetch"
        " has been kicked off, but when this flag is used it to block until"
        " all of the files are prefetched.",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "--prefetch-metadata",
        help="Prefetch file metadata (sha1 and size) for each file in a "
        + "tree when we fetch trees during this prefetch. This may send a "
        + "large amount of requests to the server and should only be used if "
        + "you understand the risks.",
        default=False,
        action="store_true",
    )
    return parser


@prefetch_profile_cmd(
    "activate",
    "Tell EdenFS to smart prefetch the files specified by the prefetch profile."
    " (EdenFS will prefetch the files in this profile immediately, when checking "
    " out a new commit and for some commits on pull).",
)
class ActivateProfileCmd(Subcmd):
    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser = add_common_args(parser)
        parser.add_argument("profile_name", help="Profile to activate.")

    def run(self, args: argparse.Namespace) -> int:
        checkout = args.checkout

        instance, checkout, _rel_path = require_checkout(args, checkout)

        with instance.get_telemetry_logger().new_sample(
                "prefetch_profile"
            ) as telemetry_sample:
            telemetry_sample.add_string("action", "activate")
            telemetry_sample.add_string("name", args.profile_name)
            telemetry_sample.add_string("checkout", args.checkout)
            telemetry_sample.add_bool("skip_prefetch", args.skip_prefetch)

            if activation_result := checkout.activate_profile(
                args.profile_name, telemetry_sample
            ):
                return activation_result

            if not args.skip_prefetch:
                result = prefetch_profiles(
                    checkout,
                    instance,
                    [args.profile_name],
                    background=not args.foreground,
                    enable_prefetch=True,
                    silent=not args.verbose,
                    revisions=None,
                    predict_revisions=False,
                    prefetch_metadata=args.prefetch_metadata,
                    predictive=False,
                    predictive_num_dirs=0,
                )
                # there will only every be one commit used to query globFiles here,
                # so no need to list which commit a file is fetched for, it will
                # be the current commit.
                if args.verbose and result is not None:
                    print_prefetch_results(result, False)
            return 0


# help=None hides this from users in `eden prefetch-profile --help`
@prefetch_profile_cmd(
    "activate-predictive",
    None,
)
class ActivatePredictiveProfileCmd(Subcmd):
    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser = add_common_args(parser)
        parser.add_argument(
            "--num-dirs",
            help="Optionally set the number of top accessed directories to"
            " prefetch, overriding the default.",
            type=check_positive_int,
            default=0,
        )

    def run(self, args: argparse.Namespace) -> int:
        checkout = args.checkout

        instance, checkout, _rel_path = require_checkout(args, checkout)

        with instance.get_telemetry_logger().new_sample(
                "prefetch_profile"
            ) as telemetry_sample:
            telemetry_sample.add_string("action", "activate-predictive")
            telemetry_sample.add_string("checkout", args.checkout)
            telemetry_sample.add_bool("skip_prefetch", args.skip_prefetch)
            if args.num_dirs:
                telemetry_sample.add_bool("num_dirs", args.num_dirs)

            if activation_result := checkout.activate_predictive_profile(
                args.num_dirs, telemetry_sample
            ):
                return activation_result

            if not args.skip_prefetch:
                try:
                    result = prefetch_profiles(
                        checkout,
                        instance,
                        [],
                        background=not args.foreground,
                        enable_prefetch=True,
                        silent=not args.verbose,
                        revisions=None,
                        predict_revisions=False,
                        prefetch_metadata=args.prefetch_metadata,
                        predictive=True,
                        predictive_num_dirs=args.num_dirs,
                    )
                    # there will only every be one commit used to query globFiles here,
                    # so no need to list which commit a file is fetched for, it will
                    # be the current commit.
                    if args.verbose and result is not None:
                        print_prefetch_results(result, False)
                    return 0
                except Exception as error:
                    # in case of a timeout or other error sending a request to the smartservice
                    # for predictive prefetch profiles, the config will be updated but fetch
                    # may not run
                    if args.verbose:
                        print(
                            (
                                f"Error in predictive fetch: {str(error)}" + "\n"
                                "Predictive prefetch is activated but fetch did not run. To retry, run: "
                                "`eden prefetch-profile fetch-predictive`"
                            )
                        )

            return 0


@prefetch_profile_cmd(
    "deactivate",
    "Tell EdenFS to STOP smart prefetching the files specified by the prefetch"
    " profile.",
)
class DeactivateProfileCmd(Subcmd):
    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("profile_name", help="Profile to deactivate.")
        parser.add_argument(
            "--checkout",
            help="The checkout for which you want to deactivate this profile.",
            default=None,
        )

    def run(self, args: argparse.Namespace) -> int:
        checkout = args.checkout

        instance, checkout, _rel_path = require_checkout(args, checkout)

        with instance.get_telemetry_logger().new_sample(
            "prefetch_profile"
        ) as telemetry_sample:
            telemetry_sample.add_string("action", "deactivate")
            telemetry_sample.add_string("name", args.profile_name)
            telemetry_sample.add_string("checkout", args.checkout)

            return checkout.deactivate_profile(args.profile_name, telemetry_sample)


# help=None hides this from users in `eden prefetch-profile --help`
@prefetch_profile_cmd(
    "deactivate-predictive",
    None,
)
class DeactivatePredictiveProfileCmd(Subcmd):
    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--checkout",
            help="The checkout for which you want to deactivate predictive prefetch.",
            default=None,
        )

    def run(self, args: argparse.Namespace) -> int:
        checkout = args.checkout
        instance, checkout, _rel_path = require_checkout(args, checkout)
        with instance.get_telemetry_logger().new_sample(
            "prefetch_profile"
        ) as telemetry_sample:
            telemetry_sample.add_string("action", "deactivate-predictive")
            telemetry_sample.add_string("checkout", args.checkout)

            return checkout.deactivate_predictive_profile(telemetry_sample)


def add_common_fetch_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    add_common_args(parser)
    parser.add_argument(
        "--commits",
        nargs="+",
        help="Commit hashes of the commits for which globs should be"
        " evaluated. Note that the current commit in the checkout is used"
        " if this is not specified. Note that the prefetch profiles are"
        " always read from the current commit, not the commits specified"
        " here.",
        default=None,
    )
    parser.add_argument(
        "--predict-commits",
        help="Predict the commits a user is likely to checkout. Evaluate"
        " the active prefetch profiles against those commits and fetch the"
        " resulting files in those commits. Note that the prefetch profiles "
        " are always read from the current commit, not the commits "
        " predicted here. This is intended to be used post pull.",
        default=False,
        action="store_true",
    )
    return parser


@prefetch_profile_cmd(
    "fetch",
    "Prefetch all the active prefetch profiles or specified prefetch profiles. "
    "This is intended for use in after checkout and pull.",
)
class FetchProfileCmd(Subcmd):
    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser = add_common_fetch_args(parser)
        parser.add_argument(
            "--profile-names",
            nargs="*",
            help="Fetch only these named profiles instead of the active set of "
            "profiles.",
            default=None,
        )

    def run(self, args: argparse.Namespace) -> int:
        checkout = args.checkout

        instance, checkout, _rel_path = require_checkout(args, checkout)

        if args.profile_names is not None:
            profiles_to_fetch = args.profile_names
        else:
            profiles_to_fetch = checkout.get_config().active_prefetch_profiles

        if not profiles_to_fetch:
            if args.verbose:
                print("No profiles to fetch.")
            return 0

        result = prefetch_profiles(
            checkout,
            instance,
            profiles_to_fetch,
            background=not args.foreground,
            enable_prefetch=not args.skip_prefetch,
            silent=not args.verbose,
            revisions=args.commits,
            predict_revisions=args.predict_commits,
            prefetch_metadata=args.prefetch_metadata,
            predictive=False,
            predictive_num_dirs=0,
        )

        if args.verbose and result is not None:
            # Can just print names it's clear which commit they come from
            # i.e. the current commit is used or only one commit passed.
            print_prefetch_results(result, args.commits and len(args.commits) > 1)

        return 0


# help=None hides this from users in `eden prefetch-profile --help`
@prefetch_profile_cmd(
    "fetch-predictive",
    None,
)
class FetchPredictiveProfileCmd(Subcmd):
    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser = add_common_fetch_args(parser)
        parser.add_argument(
            "--num-dirs",
            help="Optionally set the number of top accessed directories to"
            " prefetch. If not specified, num_dirs saved from activate-predictive"
            " or the default is used.",
            type=check_positive_int,
            default=0,
        )
        parser.add_argument(
            "--if-active",
            help="Only run the fetch if activate-predictive has been run. Uses"
            " num_dirs set by activate-predictive, or the default.",
            default=False,
            action="store_true",
        )

    def run(self, args: argparse.Namespace) -> int:

        checkout = args.checkout

        instance, checkout, _rel_path = require_checkout(args, checkout)

        # if the --if-active flag is set, don't run fetch unless predictive prefetch
        # is active in the checkout config
        if (
            args.if_active
            and not checkout.get_config().predictive_prefetch_profiles_active
        ):
            if args.verbose:
                print(
                    "Predictive prefetch profiles have not been activated and "
                    "--if-active was specified. Skipping fetch."
                )
            return 0

        # If num_dirs is given, use the specified num_dirs. If num_dirs is not given
        # (args.num_dirs == 0), predictive fetch with default num dirs unless there
        # is an active num dirs saved in the checkout config.
        predictive_num_dirs = args.num_dirs
        if (
            not predictive_num_dirs
            and checkout.get_config().predictive_prefetch_num_dirs
        ):
            predictive_num_dirs = checkout.get_config().predictive_prefetch_num_dirs
        try:
            result = prefetch_profiles(
                checkout,
                instance,
                [],
                background=not args.foreground,
                enable_prefetch=not args.skip_prefetch,
                silent=not args.verbose,
                revisions=args.commits,
                predict_revisions=args.predict_commits,
                prefetch_metadata=args.prefetch_metadata,
                predictive=True,
                predictive_num_dirs=predictive_num_dirs,
            )

            if args.verbose and result is not None:
                # Can just print names it's clear which commit they come from
                # i.e. the current commit is used or only one commit passed.
                print_prefetch_results(result, args.commits and len(args.commits) > 1)
        except Exception as error:
            # in case of a timeout or other error sending a request to the smartplatform
            # service for predictive prefetch profiles
            if args.verbose:
                print(f"Error in predictive fetch: {str(error)}")

        return 0


@prefetch_profile_cmd(
    "disable",
    "Disables prefetch profiles locally",
)
class DisableProfileCmd(Subcmd):
    def run(self, args: argparse.Namespace) -> int:
        instance = get_eden_instance(args)
        config = instance.read_local_config()
        prefetch_profiles_section = {}
        if config.has_section("prefetch-profiles"):
            prefetch_profiles_section |= config.get_section_str_to_any("prefetch-profiles")
        prefetch_profiles_section["prefetching-enabled"] = False
        config["prefetch-profiles"] = prefetch_profiles_section
        instance.write_local_config(config)

        return 0


@prefetch_profile_cmd(
    "enable",
    "Enables prefetch profiles locally",
)
class EnableProfileCmd(Subcmd):
    def run(self, args: argparse.Namespace) -> int:
        instance = get_eden_instance(args)
        config = instance.read_local_config()
        prefetch_profiles_section = {}
        if config.has_section("prefetch-profiles"):
            prefetch_profiles_section |= config.get_section_str_to_any("prefetch-profiles")
        prefetch_profiles_section["prefetching-enabled"] = True
        config["prefetch-profiles"] = prefetch_profiles_section
        instance.write_local_config(config)

        return 0


# help=None hides this from users in `eden prefetch-profile --help`
@prefetch_profile_cmd(
    "enable-predictive",
    None,
)
class EnablePredictiveProfileCmd(Subcmd):
    def run(self, args: argparse.Namespace) -> int:
        instance = get_eden_instance(args)
        config = instance.read_local_config()
        prefetch_profiles_section = {}
        if config.has_section("prefetch-profiles"):
            prefetch_profiles_section |= config.get_section_str_to_any("prefetch-profiles")
        prefetch_profiles_section["predictive-prefetching-enabled"] = True
        config["prefetch-profiles"] = prefetch_profiles_section
        instance.write_local_config(config)

        return 0


# help=None hides this from users in `eden prefetch-profile --help`
@prefetch_profile_cmd(
    "disable-predictive",
    None,
)
class DisablePredictiveProfileCmd(Subcmd):
    def run(self, args: argparse.Namespace) -> int:
        instance = get_eden_instance(args)
        config = instance.read_local_config()
        prefetch_profiles_section = {}
        if config.has_section("prefetch-profiles"):
            prefetch_profiles_section |= config.get_section_str_to_any("prefetch-profiles")
        prefetch_profiles_section["predictive-prefetching-enabled"] = False
        config["prefetch-profiles"] = prefetch_profiles_section
        instance.write_local_config(config)

        return 0


class PrefetchProfileCmd(Subcmd):
    NAME = "prefetch-profile"
    HELP = (
        "Create, manage, and use Prefetch Profiles. This command is "
        " primarily for use in automation."
    )

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        self.add_subcommands(parser, prefetch_profile_cmd.commands)

    def run(self, args: argparse.Namespace) -> int:
        return 0
