# SPDX-License-Identifier: LGPL-2.1+

import os
import re
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from mkosi.architecture import Architecture
from mkosi.archive import extract_tar
from mkosi.distributions import DistributionInstaller, PackageType
from mkosi.log import ARG_DEBUG, complete_step, die
from mkosi.run import apivfs_cmd, bwrap, chroot_cmd, run
from mkosi.state import MkosiState
from mkosi.tree import copy_tree, rmtree
from mkosi.types import PathString
from mkosi.util import flatten, sort_packages


def invoke_emerge(state: MkosiState, packages: Sequence[str] = (), apivfs: bool = True) -> None:
    bwrap(
        cmd=apivfs_cmd(state.root) + [
            # We can't mount the stage 3 /usr using `options`, because bwrap isn't available in the stage 3
            # tarball which is required by apivfs_cmd(), so we have to mount /usr from the tarball later
            # using another bwrap exec.
            "bwrap",
            "--dev-bind", "/", "/",
            "--bind", state.cache_dir / "stage3/usr", "/usr",
            "emerge",
            "--buildpkg=y",
            "--usepkg=y",
            "--getbinpkg=y",
            "--binpkg-respect-use=y",
            "--jobs",
            "--load-average",
            "--root-deps=rdeps",
            "--with-bdeps=n",
            "--verbose-conflicts",
            "--noreplace",
            *(["--verbose", "--quiet=n", "--quiet-fail=n"] if ARG_DEBUG.get() else ["--quiet-build", "--quiet"]),
            f"--root={state.root}",
            *sort_packages(packages),
        ],
        network=True,
        options=[
            # TODO: Get rid of as many of these as possible.
            "--bind", state.cache_dir / "stage3/etc", "/etc",
            "--bind", state.cache_dir / "stage3/var", "/var",
            "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf",
            "--bind", state.cache_dir / "repos", "/var/db/repos",
            *flatten(["--bind", str(d), str(d)] for d in (state.config.workspace_dir, state.config.cache_dir) if d),
        ],
        env=dict(
            PKGDIR=str(state.cache_dir / "binpkgs"),
            DISTDIR=str(state.cache_dir / "distfiles"),
        ) | ({"USE": "build"} if not apivfs else {}) | state.config.environment,
    )


class GentooInstaller(DistributionInstaller):
    @classmethod
    def filesystem(cls) -> str:
        return "btrfs"

    @classmethod
    def package_type(cls) -> PackageType:
        return PackageType.ebuild

    @classmethod
    def setup(cls, state: MkosiState) -> None:
        pass

    @classmethod
    def install(cls, state: MkosiState) -> None:
        arch = state.config.distribution.architecture(state.config.architecture)

        assert state.config.mirror
        # http://distfiles.gentoo.org/releases/amd64/autobuilds/latest-stage3.txt
        stage3tsf_path_url = urllib.parse.urljoin(
            state.config.mirror.partition(" ")[0],
            f"releases/{arch}/autobuilds/latest-stage3.txt",
        )

        with urllib.request.urlopen(stage3tsf_path_url) as r:
            # e.g.: 20230108T161708Z/stage3-amd64-nomultilib-systemd-mergedusr-20230108T161708Z.tar.xz
            regexp = rf"^[0-9]+T[0-9]+Z/stage3-{arch}-llvm-systemd-mergedusr-[0-9]+T[0-9]+Z\.tar\.xz"
            all_lines = r.readlines()
            for line in all_lines:
                if (m := re.match(regexp, line.decode("utf-8"))):
                    stage3_latest = Path(m.group(0))
                    break
            else:
                die("profile names changed upstream?")

        stage3_url = urllib.parse.urljoin(state.config.mirror, f"releases/{arch}/autobuilds/{stage3_latest}")
        stage3_tar = state.cache_dir / "stage3.tar"
        stage3 = state.cache_dir / "stage3"

        with complete_step("Fetching latest stage3 snapshot"):
            old = stage3_tar.stat().st_mtime if stage3_tar.exists() else 0

            cmd: list[PathString] = ["curl", "-L", "--progress-bar", "-o", stage3_tar, stage3_url]
            if stage3_tar.exists():
                cmd += ["--time-cond", stage3_tar]

            run(cmd)

            if stage3_tar.stat().st_mtime > old:
                rmtree(stage3)

        stage3.mkdir(exist_ok=True)

        if not any(stage3.iterdir()):
            with complete_step(f"Extracting {stage3_tar.name} to {stage3}"):
                extract_tar(stage3_tar, stage3)

        for d in ("binpkgs", "distfiles", "repos/gentoo"):
            (state.cache_dir / d).mkdir(parents=True, exist_ok=True)

        copy_tree(state.config, state.pkgmngr, stage3, preserve_owner=False)

        features = " ".join([
            # Disable sandboxing in emerge because we already do it in mkosi.
            "-sandbox",
            "-pid-sandbox",
            "-ipc-sandbox",
            "-network-sandbox",
            "-userfetch",
            "-userpriv",
            "-usersandbox",
            "-usersync",
            "-ebuild-locks",
            "parallel-install",
            *(["noman", "nodoc", "noinfo"] if state.config.with_docs else []),
        ])

        # Setting FEATURES via the environment variable does not seem to apply to ebuilds in portage, so we
        # append to /etc/portage/make.conf instead.
        with (stage3 / "etc/portage/make.conf").open("a") as f:
            f.write(f"\nFEATURES=\"${{FEATURES}} {features}\"\n")

        chroot = chroot_cmd(
            stage3,
            options=["--bind", state.cache_dir / "repos", "/var/db/repos"],
        )

        bwrap(cmd=chroot + ["emerge-webrsync"], network=True,
              options=flatten(["--bind", d, d] for d in (state.config.workspace_dir, state.config.cache_dir) if d))

        invoke_emerge(state, packages=["sys-apps/baselayout"], apivfs=False)

    @classmethod
    def install_packages(cls, state: MkosiState, packages: Sequence[str], apivfs: bool = True) -> None:
        invoke_emerge(state, packages=packages, apivfs=apivfs)

        for d in state.root.glob("usr/src/linux-*"):
            kver = d.name.removeprefix("linux-")
            kimg = d / {
                Architecture.x86_64: "arch/x86/boot/bzImage",
                Architecture.arm64: "arch/arm64/boot/Image.gz",
                Architecture.arm: "arch/arm/boot/zImage",
            }[state.config.architecture]
            vmlinuz = state.root / "usr/lib/modules" / kver / "vmlinuz"
            if not vmlinuz.exists() and not vmlinuz.is_symlink():
                vmlinuz.symlink_to(os.path.relpath(kimg, start=vmlinuz.parent))

    @staticmethod
    def architecture(arch: Architecture) -> str:
        a = {
            Architecture.x86_64 : "amd64",
            Architecture.arm64  : "arm64",
            Architecture.arm    : "arm",
        }.get(arch)

        if not a:
            die(f"Architecture {a} is not supported by Gentoo")

        return a
