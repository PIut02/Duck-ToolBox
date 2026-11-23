#!/usr/bin/env python3
"""Build the embedded Play Integrity component and composite module payload."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tarfile
import urllib.request

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
ENGINE = ROOT / "components/play-integrity"
VENDOR = ROOT / "third-party/native" if (ROOT / "third-party/native").is_dir() else ROOT / "target/play-integrity/vendor"
DEPENDENCIES = {
    "json": ("nlohmann/json", "55f93686c01528224f448c19128836e7df245f72"),
    "local_cxa_atexit_finalize_impl": (
        "5ec1cff/local_cxa_atexit_finalize_impl", "b269136606d6f230089fbf47a0b219f7f2ccef3c"
    ),
    "shadowhook": ("bytedance/android-inline-hook", "854c775c2c3676e57a0f383597ebf420b5204161"),
    # x86_64 has no ShadowHook backend; Dobby supplies the inline hook there.
    "Dobby": ("jmpews/Dobby", "5dfc8546954ce3b3198132ab13fddb89ee92cdd7"),
    "hiddenapibypass": ("LSPosed/AndroidHiddenApiBypass", "4efc64fcada7df15e7b5ba068e774343b760270d"),
    "lsplant": ("LSPosed/LSPlant", "6ac2280d8d88e14f0864c26ac97c1dc96713bde1"),
    "dex_builder": ("LSPosed/DexBuilder", "ac7fb2230954ee311808bad469b0db501f31bfb8"),
    "parallel_hashmap": ("greg7mdp/parallel-hashmap", "0cd57d29a959256ed66b2afdd1009928fc625d09"),
}


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def dependencies():
    VENDOR.mkdir(parents=True, exist_ok=True)
    for name, (repo, commit) in DEPENDENCIES.items():
        destination = VENDOR / name
        if (destination / ".revision").exists() and (destination / ".revision").read_text() == commit:
            continue
        archive = VENDOR / f"{name}-{commit}.tar.gz"
        if not archive.exists():
            with urllib.request.urlopen(f"https://codeload.github.com/{repo}/tar.gz/{commit}", timeout=60) as response:
                archive.write_bytes(response.read())
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir()
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                parts = Path(member.name).parts[1:]
                if not parts:
                    continue
                member.name = "/".join(parts)
                source.extract(member, destination, filter="data")
        (destination / ".revision").write_text(commit)
    # Materialize the pinned LSPlant submodules without depending on a Git checkout.
    for source, destination in [
        ("dex_builder", "lsplant/lsplant/src/main/jni/external/dex_builder"),
        ("parallel_hashmap", "lsplant/lsplant/src/main/jni/external/dex_builder/external/parallel_hashmap"),
    ]:
        target = VENDOR / destination
        if not (target / ".revision").exists() or (target / ".revision").read_text() != (VENDOR / source / ".revision").read_text():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(VENDOR / source, target)
    # PIF deliberately disables shadowhook's linker initialization. Keep the patch
    # reproducible from this build script and its pinned upstream revision.
    linker = VENDOR / "shadowhook/shadowhook/src/main/cpp/sh_linker.c"
    value = linker.read_text()
    if "sh_linker_init_dummy" not in value:
        linker.write_text(value.replace("sh_linker_init", "sh_linker_init_dummy"))
    symbols = VENDOR / "shadowhook/shadowhook/src/main/cpp/third_party/xdl/xdl.c"
    value = symbols.read_text()
    if '#include "xdl-prefix.inc"' not in value:
        symbols.write_text(value + '\n// Modified 2026-10-31: expose ART prefix lookup.\n#include "xdl-prefix.inc"\n')
    patch_dobby()


def patch_dobby():
    """Make the pinned Dobby buildable for Android x86_64.

    Upstream lists every architecture's .asm unconditionally, so the arm64
    trampolines are fed to the x86_64 assembler and fail. Two more upstream
    inconsistencies only surface on x86_64: an include cycle that hides OSMemory,
    and RuntimeModule.load_address (the struct field is named base).
    """
    root = VENDOR / "Dobby"
    cmake = root / "CMakeLists.txt"
    value = cmake.read_text()
    if "# Duck: ARM assembly excluded on x86_64" not in value:
        value = value.replace(
            "  source/TrampolineBridge/ClosureTrampolineBridge/arm64/closure_bridge_arm64.asm\n"
            "  source/TrampolineBridge/ClosureTrampolineBridge/arm64/closure_trampoline_arm64.asm\n",
            "  # Duck: ARM assembly excluded on x86_64\n")
        cmake.write_text(value)
    runtime = root / "source/Backend/UserMode/PlatformUtil/Linux/ProcessRuntime.cc"
    value = runtime.read_text()
    if "DUCK_RUNTIME_MODULE_BASE" not in value:
        value = value.replace("  return (a.start < b.start);",
                              "  return (a.start() < b.start());  // DUCK_RUNTIME_MODULE_BASE")
        value = value.replace("RuntimeModule module = {0};",
                              "RuntimeModule module = {0};  // DUCK_RUNTIME_MODULE_BASE")
        value = value.replace("module.load_address", "module.base")
        runtime.write_text(value)
    # The same stale field name also appears in the Android symbol resolver.
    symres = root / "builtin-plugin/SymbolResolver/elf/dobby_symbol_resolver.cc"
    value = symres.read_text()
    if "module.load_address" in value:
        symres.write_text(value.replace("module.load_address", "module.base"))
    code_patch = root / "source/Backend/UserMode/ExecMemory/code-patch-tool-posix.cc"
    value = code_patch.read_text()
    if "DUCK_DROP_CPU_INCLUDE" not in value:
        # core/arch/Cpu.h does not exist in this revision; dobby_internal.h already
        # provides ALIGN_FLOOR, addr_t and ClearCache.
        value = value.replace('#include "core/arch/Cpu.h"',
                              '// DUCK_DROP_CPU_INCLUDE: header absent; declarations come from dobby_internal.h')
        code_patch.write_text(value)
    features = root / "common/os_arch_features.h"
    value = features.read_text()
    if "DUCK_NO_ARCH_MEMORY" not in value:
        value = value.replace("#if defined(ANDROID)\n  auto page",
                              "#if 0  // DUCK_NO_ARCH_MEMORY: include cycle hides OSMemory; unused on x86_64\n  auto page")
        features.write_text(value)
    # x64 trampolines use #if/#define, so their .asm needs the C preprocessor.
    # Upstream only sets this for ARM/AARCH64.
    compiler = root / "cmake/compiler_and_linker.cmake"
    value = compiler.read_text()
    if "DUCK_ASM_CPP" not in value:
        value = value.replace(
            "elseif (PROCESSOR.AARCH64)\n  set(CMAKE_ASM_FLAGS \"${CMAKE_ASM_FLAGS} -x assembler-with-cpp\")\nendif ()",
            "elseif (PROCESSOR.AARCH64)\n  set(CMAKE_ASM_FLAGS \"${CMAKE_ASM_FLAGS} -x assembler-with-cpp\")\n"
            "elseif (PROCESSOR.X86_64)\n  set(CMAKE_ASM_FLAGS \"${CMAKE_ASM_FLAGS} -x assembler-with-cpp\")  # DUCK_ASM_CPP\nendif ()")
        compiler.write_text(value)


def source_revision():
    """The exact Git revision used for this engine build, not a moving branch."""
    marker = ROOT / "SOURCE_COMMIT"
    if marker.is_file():
        return marker.read_text().strip()
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-only", action="store_true")
    parser.add_argument("--dependencies-only", action="store_true")
    parser.add_argument("--sdk", default=os.environ.get("ANDROID_HOME", str(Path.home() / "Library/Android/sdk")))
    args = parser.parse_args()
    dependencies()
    if args.dependencies_only:
        return
    env = {**os.environ, "PIF_VENDOR_DIR": str(VENDOR), "ANDROID_HOME": args.sdk}
    run("cargo", "build", "--locked", "--manifest-path", ENGINE / "Cargo.toml", "--target-dir", ROOT / "target/pif-engine", env=env)
    if args.host_only:
        return
    ndk = Path(args.sdk) / "ndk/30.0.16248370"
    if not ndk.is_dir():
        raise SystemExit(f"Install the pinned NDK first: sdkmanager 'ndk;{ndk.name}'")
    toolchains = list((ndk / "toolchains/llvm/prebuilt").glob("*/bin"))
    if not toolchains:
        raise SystemExit("NDK host toolchain missing")
    toolchain = toolchains[0]
    target = "aarch64-linux-android"
    linker = toolchain / (f"{target}26-clang.cmd" if os.name == "nt" else f"{target}26-clang")
    env.update({
        "CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER": str(linker),
        "CC_aarch64_linux_android": str(linker),
        "AR_aarch64_linux_android": str(toolchain / ("llvm-ar.exe" if os.name == "nt" else "llvm-ar")),
    })
    run("cargo", "build", "--locked", "--release", "--manifest-path", ENGINE / "Cargo.toml", "--target-dir", ROOT / "target/pif-engine", "--target", target, env=env)
    run(sys.executable, ROOT / "scripts/build-device-faker.py", "--sdk", args.sdk)

    # x86_64/WSA: build the same components for the second ABI into separate dirs.
    x86_target = "x86_64-linux-android"
    x86_linker = toolchain / (f"{x86_target}26-clang.cmd" if os.name == "nt" else f"{x86_target}26-clang")
    env.update({
        "CARGO_TARGET_X86_64_LINUX_ANDROID_LINKER": str(x86_linker),
        "CC_x86_64_linux_android": str(x86_linker),
        "AR_x86_64_linux_android": str(toolchain / ("llvm-ar.exe" if os.name == "nt" else "llvm-ar")),
    })
    run("cargo", "build", "--locked", "--release", "--manifest-path", ENGINE / "Cargo.toml", "--target-dir", ROOT / "target/pif-engine-x86_64", "--target", x86_target, env=env)
    run(sys.executable, ROOT / "scripts/build-device-faker.py", "--sdk", args.sdk, "--android-abi", "x86_64")

    env.update({"DUCK_RUNTIME_DIR": str(ROOT / "components/zygisk-runtime"),
                "DUCK_LANGUAGE_DIR": str(ROOT / "components/language-spoof"),
                "DUCK_FAKER_LIBRARY": str(ROOT / "target/device-faker/libdevicefaker.a"),
                "DUCK_FAKER_LIBRARY_X86_64": str(ROOT / "target/device-faker/libdevicefaker-x86_64.a"),
                "DUCK_FAKER_JAVA": str(ROOT / "components/device-faker/java")})
    wrapper = ENGINE / "android/gradlew"
    gradle_command = [wrapper.with_suffix(".bat")] if os.name == "nt" else ["sh", wrapper]
    run(*gradle_command, "--no-daemon", "assembleRelease", cwd=wrapper.parent, env=env)
    payload = ROOT / "module/play-integrity"
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ENGINE / "scripts", payload / "scripts", dirs_exist_ok=True)
    shutil.copy2(ROOT / f"target/pif-engine/{target}/release/pif-engine", payload / "pif-engine")
    shutil.copy2(ROOT / f"target/pif-engine-x86_64/{x86_target}/release/pif-engine", payload / "pif-engine-x86-64")
    dex = ENGINE / "android/app/build/intermediates/dex/release/minifyReleaseWithR8/classes.dex"
    shutil.copy2(dex, payload / "classes.dex")
    (payload / "language-spoof.version").write_text("1\n")
    libraries = ENGINE / "android/app/build/intermediates/stripped_native_libs/release/stripReleaseDebugSymbols/out/lib"
    zygisk = ROOT / "module/zygisk"
    if zygisk.exists():
        shutil.rmtree(zygisk)
    zygisk.mkdir(exist_ok=True)
    for abi in ["arm64-v8a", "armeabi-v7a", "x86_64"]:
        shutil.copy2(libraries / abi / "libplayintegrityfix.so", zygisk / f"{abi}.so")
    notices = ROOT / "module/licenses/play-integrity"
    if notices.exists():
        shutil.rmtree(notices)
    notices.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ENGINE / "LICENSE", notices / "GPL-3.0.txt")
    for name in ["README.md", "RULES.md", "NOTICE", "UPSTREAM.json"]:
        shutil.copy2(ENGINE / name, notices / name)
    for name in DEPENDENCIES:
        for path in (VENDOR / name).rglob("*"):
            if path.is_file() and ("LICENSE" in path.name.upper() or path.name.upper()=="NOTICE"):
                destination=notices / name / path.relative_to(VENDOR / name)
                destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(path,destination)
    language_notices = ROOT / "module/licenses/language-spoof"
    language_notices.mkdir(parents=True, exist_ok=True)
    for name in ["LICENSE", "NOTICE", "README.md"]:
        shutil.copy2(ROOT / "components/language-spoof" / name, language_notices / name)
    # Maven dependencies' license texts are kept with the source component.
    shutil.copytree(ENGINE / "licenses", notices, dirs_exist_ok=True)
    # Source links identify the same immutable commit as the engine payload.
    commit = source_revision()
    if commit:
        source_url = f"https://github.com/eltavine/Duck-ToolBox/tree/{commit}/components/play-integrity"
        (notices / "SOURCE.txt").write_text(
            f"Corresponding Play Integrity GPLv3 source (Git commit {commit}):\n"
            f"{source_url}\n"
            "Build and dependency retrieval script:\n"
            f"https://github.com/eltavine/Duck-ToolBox/blob/{commit}/scripts/build-play-integrity.py\n"
            "This script retrieves pinned dependency sources and applies the recorded vendor changes.\n"
        )
    (notices.parent / "play-integrity-source.tar.gz").unlink(missing_ok=True)
    (ROOT / "module/webroot/licenses/play-integrity-source.tar.gz").unlink(missing_ok=True)
    artifacts = [*(p for p in payload.rglob("*") if p.is_file()), *zygisk.glob("*.so")]
    (payload / "build.json").write_text(json.dumps({
        "upstream_revision": json.loads((ENGINE / "UPSTREAM.json").read_text())["revision"],
        "source_commit": commit,
        "embedded": {"api": 4, "language_contract": 1, "device_faker_revision": (ROOT / "components/device-faker/UPSTREAM_REVISION").read_text().strip()},
        "files": {p.relative_to(ROOT / "module").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts},
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
