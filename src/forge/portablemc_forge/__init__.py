from argparse import ArgumentParser, Namespace
from typing import Dict, Set, Optional
from zipfile import ZipFile
from io import BytesIO
from os import path
import subprocess
import shutil
import json
import sys
import os

from portablemc import Version, \
    LibrarySpecifier, \
    DownloadEntry, \
    BaseError, \
    replace_vars, \
    http_request, json_simple_request, Context


def load():

    from portablemc.cli import CliContext
    from portablemc import cli as pmc

    # Private mixins

    @pmc.mixin()
    def register_start_arguments(old, parser: ArgumentParser):
        _ = pmc.get_message
        parser.add_argument("--forge-prefix", help=_("args.start.forge_prefix"), default="forge", metavar="PREFIX")
        old(parser)

    @pmc.mixin()
    def cmd_start(old, ns: Namespace, ctx: CliContext):
        try:
            return old(ns, ctx)
        except ForgeError as err:
            pmc.print_task("FAILED", f"start.forge.error.{err.code}", {"version": err.version}, done=True)
            sys.exit(pmc.EXIT_VERSION_NOT_FOUND)

    @pmc.mixin()
    def new_version(old, ctx: CliContext, version_id: str) -> Version:

        if version_id.startswith("forge:"):

            game_version = version_id[6:]
            if not len(game_version):
                game_version = "release"

            manifest = pmc.new_version_manifest(ctx)
            game_version, game_version_alias = manifest.filter_latest(game_version)

            forge_version = None

            # If the version is an alias, we know that the version needs to be resolved from the forge
            # promotion metadata. It's also the case if the version ends with '-recommended' or '-latest',
            # or if the version doesn't contains a "-".
            if game_version_alias or game_version.endswith(("-recommended", "-latest")) or "-" not in game_version:
                promo_versions = request_promo_versions()
                for suffix in ("", "-recommended", "-latest"):
                    tmp_forge_version = promo_versions.get(f"{game_version}{suffix}")
                    if tmp_forge_version is not None:
                        if game_version.endswith("-recommended"):
                            game_version = game_version[:-12]
                        elif game_version.endswith("-latest"):
                            game_version = game_version[:-7]
                        forge_version = f"{game_version}-{tmp_forge_version}"
                        break

            if forge_version is None:
                # If the game version came from an alias, we know for sure that no forge
                # version is currently supporting the latest release/snapshot.
                if game_version_alias:
                    raise ForgeError(ForgeError.MINECRAFT_VERSION_NOT_SUPPORTED, game_version)
                # Test if the user has given the full forge version
                forge_version = game_version

            return ForgeVersion(ctx, forge_version, prefix=ctx.ns.forge_prefix)

        return old(ctx, version_id)

    # Messages

    pmc.messages.update({
        "args.start.forge_prefix": "Change the prefix of the version ID when starting with Forge.",
        "start.forge.resolving": "Resolving forge {version}...",
        "start.forge.resolved": "Resolved forge {version}, downloading installer and parent version.",
        "start.forge.wrapper.running": "Running installer (can take few minutes)...",
        "start.forge.wrapper.done": "Forge installation done.",
        "start.forge.consider_support": "Consider supporting the forge project through https://www.patreon.com/LexManos/.",
        f"start.forge.error.{ForgeError.INSTALLER_NOT_FOUND}": "No installer found for forge {version}.",
        f"start.forge.error.{ForgeError.MINECRAFT_VERSION_NOT_FOUND}": "Parent Minecraft version not found "
                                                                                 "{version}.",
        f"start.forge.error.{ForgeError.MINECRAFT_VERSION_NOT_SUPPORTED}": "Minecraft version {version} is not "
                                                                                     "currently supported by forge."
    })


class ForgeVersion(Version):

    def __init__(self, context: Context, forge_version: str, *, prefix: str = "forge"):
        
        super().__init__(context, f"{prefix}-{forge_version}")
        self.forge_version = forge_version
        
        # These fields are used when the version is being installed.
        self.forge_install_libraries = None
        self.forge_install_processors = None
        self.forge_install_data = None
        self.forge_install_version_libraries = None

    def _validate_version_meta(self, version_id: str, version_dir: str, version_meta_file: str, version_meta: dict) -> bool:
        if version_id == self.id:
            # TODO: Various checks for presence of libs.
            return True
        else:
            return super()._validate_version_meta(version_id, version_dir, version_meta_file, version_meta)

    def _fetch_version_meta(self, version_id: str, version_dir: str, version_meta_file: str) -> dict:
        
        if version_id != self.id:
            return super()._fetch_version_meta(version_id, version_dir, version_meta_file)
        
        # Extract the game version from the forge version, we'll use
        # it to add suffix to find the right forge version if needed.
        game_version = self.forge_version.split("-", 1)[0]

        # For some older game versions, some odd suffixes where used 
        # for the version scheme.
        suffixes = [""]
        suffixes.extend({
            "1.11":     ("-1.11.x",),
            "1.10.2":   ("-1.10.0",),
            "1.10":     ("-1.10.0",),
            "1.9.4":    ("-1.9.4",),
            "1.9":      ("-1.9.0", "-1.9"),
            "1.8.9":    ("-1.8.9",),
            "1.8.8":    ("-1.8.8",),
            "1.8":      ("-1.8",),
            "1.7.10":   ("-1.7.10", "-1710ls", "-new"),
            "1.7.2":    ("-mc172",),
        }.get(game_version, []))

        # Iterate suffix and find the first install JAR that works.
        for suffix in suffixes:
            install_jar = request_install_jar(f"{self.forge_version}{suffix}")
            if install_jar is not None:
                break

        if install_jar is None:
            raise ForgeError(ForgeError.INSTALLER_NOT_FOUND, self.forge_version)

        with install_jar:

            # The install profiles comes in multiples forms:
            # 
            # >= 1.12.2-14.23.5.2851
            #  There are two files, 'install_profile.json' which 
            #  contains processors and shared data, and `version.json`
            #  which is the raw version meta to be fetched.
            #
            # <= 1.12.2-14.23.5.2847
            #  There is only an 'install_profile.json' with the version
            #  meta stored in 'versionInfo' object. Each library have
            #  two keys 'serverreq' and 'clientreq' that should be
            #  removed when the profile is returned.

            try:
                info = install_jar.getinfo("install_profile.json")
                with install_jar.open(info) as fp:
                    install_profile = json.load(fp)
            except KeyError:
                raise ForgeError(ForgeError.INSTALL_PROFILE_NOT_FOUND, self.forge_version)

            if "json" in install_profile:

                # Forge versions since 1.12.2-14.23.5.2851
                info = install_jar.getinfo(install_profile["json"].lstrip("/"))
                with install_jar.open(info) as fp:
                    version_meta = json.load(fp)

                self.forge_install_version_libraries = version_meta["libraries"]
                self.forge_install_processors = install_profile["processors"]
                self.forge_install_data = {}
                self.forge_install_libraries = {}
            
                # We fetch all libraries used to build artifacts, and
                # we store each path to each library here.
                for install_lib in install_profile["libraries"]:

                    lib_name = install_lib["name"]
                    lib_spec = LibrarySpecifier.from_str(lib_name)
                    lib_artifact = install_lib["downloads"]["artifact"]
                    lib_path = path.join(self.context.libraries_dir, lib_spec.file_path())

                    self.forge_install_libraries[lib_name] = lib_path
                    
                    if len(lib_artifact["url"]):
                        lib_dl_entry = DownloadEntry.from_meta(lib_artifact, lib_path)
                        if not path.isfile(lib_path) or (lib_dl_entry.size is not None and path.getsize(lib_path) != lib_dl_entry.size):
                            self.dl.append(lib_dl_entry)
                    else:
                        # The lib should be stored inside the JAR file, under maven/ directory.
                        zip_extract_file(install_jar, f"maven/{lib_spec.file_path()}", lib_path)

                # Just keep the 'client' values.
                for data_key, data_val in install_profile["data"].items():
                    self.forge_install_data[data_key] = data_val["client"]
                
            else: 

                # Forge versions before 1.12.2-14.23.5.2847
                version_meta = install_profile.get("versionInfo")
                if version_meta is None:
                    raise ForgeError(ForgeError.VERSION_META_NOT_FOUND, self.forge_version)
                
                # Older versions have non standard keys for libraries.
                for version_lib in version_meta["libraries"]:
                    if "serverreq" in version_lib:
                        del version_lib["serverreq"]
                    if "clientreq" in version_lib:
                        del version_lib["clientreq"]
                    if "checksums" in version_lib:
                        del version_lib["checksums"]
                
                # For "old" installers, that have an "install" section.
                jar_entry_path = install_profile["install"]["filePath"]
                jar_spec = LibrarySpecifier.from_str(install_profile["install"]["path"])

                # Here we copy the forge jar stored to libraries.
                jar_path = path.join(self.context.libraries_dir, jar_spec.file_path())
                zip_extract_file(install_jar, jar_entry_path, jar_path)

            version_meta["id"] = version_id
            return version_meta
    
    def prepare_libraries(self, *, predicate = None):

        # When installing, we opt-out the libraries that have no URL,
        # because these will be generated by just after the download
        # and we don't want errors if they are not present.
        if self.forge_install_libraries is not None:
            self.version_meta["libraries"] = [
                lib for lib in self.version_meta["libraries"] if lib.get("downloads")
            ]

        return super().prepare_libraries(predicate=predicate)

    def prepare_post(self) -> None:

        super().prepare_post()

        # If the modern installer need to be ran.
        if self.install_lib_paths is not None:

            if len(self.install_processors) and self.jvm_exec is None:
                raise ForgeError(ForgeError.REQUIRES_JVM, self.forge_version)

            def replace_install_vars(txt: str) -> str:
                # Replace the pattern [lib name] with lib path.
                if txt[0] == "[" and txt[-1] == "]":
                    return self.install_lib_paths[txt[1:-1]]
                # Fallback to a standard 
                return replace_vars(txt, self.install_data)

            for processor in self.install_processors:

                jar_name = processor["jar"]
                jar_path = self.install_lib_paths[jar_name]
                classpath = [self.install_lib_paths[lib_name] for lib_name in processor.get("classpath", [])]
                args = [replace_install_vars(arg) for arg in processor.get("args", [])]

                all_args = [self.jvm_exec, "-cp", path.pathsep.join(classpath), "-jar", jar_path, *args]
                process = subprocess.run(all_args)

                print(f"{all_args}: {process}")
                


# Forge API

def request_promo_versions() -> Dict[str, str]:
    raw = json_simple_request("https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json")
    return raw["promos"]


def request_maven_versions() -> Optional[Set[str]]:

    status, raw = http_request("https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml", "GET", headers={
        "Accept": "application/xml"
    })

    if status != 200:
        return None

    text = raw.decode()
    versions = set()
    last_idx = 0

    # It's not really correct to parse XML like this, but I find this
    # acceptable since the schema is well known and it should be a
    # little bit easier to do thing like this.
    while True:
        start_idx = text.find("<version>", last_idx)
        if start_idx == -1:
            break
        end_idx = text.find("</version>", start_idx + 9)
        if end_idx == -1:
            break
        versions.add(text[(start_idx + 9):end_idx])
        last_idx = end_idx + 10

    return versions


def request_install_jar(version: str) -> Optional[ZipFile]:
    status, raw = http_request(f"https://maven.minecraftforge.net/net/minecraftforge/forge/{version}/forge-{version}-installer.jar", "GET", headers={
        "Accept": "application/java-archive"
    })
    return ZipFile(BytesIO(raw)) if status == 200 else None


def zip_extract_file(zf: ZipFile, entry_path: str, dst_path: str):
    """ 
    Special function used to extract a specific file entry to a 
    destination. This is different from ZipFile.extract because
    the latter keep the full entry's path.
    """
    os.makedirs(path.dirname(dst_path), exist_ok=True)
    with zf.open(entry_path) as src, open(dst_path, "wb") as dst:
        shutil.copyfileobj(src, dst)


# Errors

class ForgeError(BaseError):

    NOT_INSTALLED = "not_installed"
    MINECRAFT_VERSION_NOT_FOUND = "minecraft_version_not_found"  # DEPRECATED
    MINECRAFT_VERSION_NOT_SUPPORTED = "minecraft_version_not_supported"

    INSTALLER_NOT_FOUND = "installer_not_found"
    INSTALL_PROFILE_NOT_FOUND = "install_profile_not_found"
    VERSION_META_NOT_FOUND = "version_meta_not_found"
    REQUIRES_JVM = "requires_jvm"

    def __init__(self, code: str, version: str):
        super().__init__(code)
        self.version = version
