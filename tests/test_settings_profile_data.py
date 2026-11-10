"""The shipped emulator settings data: structure, and what its lists make of real key names.

The fixtures are small synthetic files with the structure and key names the emulators use
(fake values only). Reading them with the file's own handler gives the canonical keys, so a
pattern written in the wrong form (`Key` for `[Section]Key`, a Qt `\\` lost) fails here.
"""

import unittest

from warpzone.profiles import SettingsFile, load_profile
from warpzone.settings_formats import handler, is_builtin_secret, match

CATEGORIES = ("noise", "secret", "device", "known")

EXPECTED_FILES = {
    ("eden", "qt-config"): ("~/.config/eden/qt-config.ini", "qt-ini"),
    (
        "retroarch",
        "cfg",
    ): ("~/.var/app/org.libretro.RetroArch/config/retroarch/retroarch.cfg", "retroarch-cfg"),
    (
        "retroarch",
        "opt",
    ): (
        "~/.var/app/org.libretro.RetroArch/config/retroarch/config/{name}/{name}.opt",
        "retroarch-cfg",
    ),
    ("duckstation", "settings"): ("~/.local/share/duckstation/settings.ini", "ini"),
    ("pcsx2", "ini"): ("~/.config/PCSX2/inis/PCSX2.ini", "ini"),
    (
        "dolphin",
        "dolphin",
    ): ("~/.var/app/org.DolphinEmu.dolphin-emu/config/dolphin-emu/Dolphin.ini", "ini"),
    (
        "dolphin",
        "gfx",
    ): ("~/.var/app/org.DolphinEmu.dolphin-emu/config/dolphin-emu/GFX.ini", "ini"),
    ("azahar", "qt-config"): ("~/.config/azahar-emu/qt-config.ini", "qt-ini"),
    (
        "melonds",
        "config",
    ): ("~/.var/app/net.kuribo64.melonDS/config/melonDS/melonDS.toml", "toml"),
    (
        "ppsspp",
        "ini",
    ): ("~/.var/app/org.ppsspp.PPSSPP/config/ppsspp/PSP/SYSTEM/ppsspp.ini", "ini"),
    ("vita3k", "config"): ("~/.config/Vita3K/config.yml", "yaml-flat"),
    ("xemu", "config"): ("~/.var/app/app.xemu.xemu/data/xemu/xemu/xemu.toml", "toml"),
    ("ryujinx", "config"): ("~/.config/Ryujinx/Config.json", "json"),
    ("xenia", "config"): ("~/Emulation/roms/xbox360/xenia-canary.config.toml", "toml"),
    ("shadps4", "config"): ("~/.local/share/shadPS4/config.json", "json"),
    ("shadps4", "launcher"): ("~/.local/share/shadPS4QtLauncher/qt_ui.ini", "ini"),
}

# What shadPS4 v0.21 writes in config.json, and what its Qt launcher writes in qt_ui.ini.
SHADPS4_CONFIG = (
    "{\n"
    '  "General": {\n'
    '    "install_dirs": [{"enabled": true, "path": "/fake/games"}],\n'
    '    "addon_install_dir": "/fake/dlc",\n'
    '    "home_dir": "",\n'
    '    "volume_slider": 100,\n'
    '    "console_language": 1,\n'
    '    "big_picture_scale": 1000\n'
    "  },\n"
    '  "Input": {"cursor_state": 1},\n'
    '  "GPU": {"window_width": 1280, "full_screen": false, "fsr_enabled": true},\n'
    '  "Vulkan": {"gpu_id": -1}\n'
    "}\n"
)
SHADPS4_LAUNCHER = (
    "[general_settings]\n"
    "guiLanguage=en_US\n"
    "shadPath=/fake/emu\n"
    "theme=0\n"
    "[main_window]\n"
    "geometry=@ByteArray(fake)\n"
    "[version_manager]\n"
    "versionSelected=/fake/version\n"
)


# Canonical key -> the lists it must fall in ("plain" for none of them).
FIXTURES: dict[tuple[str, str], tuple[str, dict[str, str | set[str]]]] = {
    ("eden", "qt-config"): (
        "[Controls]\n"
        "player_0_button_a=fake\n"
        "[Data%20Storage]\n"
        "nand_directory=/fake/nand\n"
        "[Renderer]\n"
        "resolution_setup=2\n"
        "resolution_setup\\default=false\n"
        "backend=1\n"
        "gpu_accuracy=1\n"
        "vulkan_device=0\n"
        "max_anisotropy=3\n"
        "use_vsync=true\n"
        "[System]\n"
        "device_name=Fake Console\n"
        "use_docked_mode=1\n"
        "language_index=1\n"
        "region_index=2\n"
        "rng_seed=1\n"
        "[UI]\n"
        "Paths\\language=es\n"
        "Paths\\recentFiles=fake\n"
        "Paths\\gamedirs\\1\\path=/fake/roms\n"
        "UILayout\\geometry=fake\n"
        "Shortcuts\\Main%20Window\\Exit\\KeySeq=Ctrl+Q\n"
        "[WebService]\n"
        "eden_token=fake\n"  # gitleaks:allow
        "eden_username=fake\n"
        "web_api_url=https://fake.invalid\n",
        {
            "[Controls]player_0_button_a": "noise",
            "[Data%20Storage]nand_directory": "noise",
            "[Renderer]resolution_setup": "device",
            "[Renderer]backend": "known",
            "[Renderer]gpu_accuracy": "known",
            "[Renderer]vulkan_device": "noise",
            "[Renderer]max_anisotropy": "device",
            "[Renderer]use_vsync": "plain",
            "[System]device_name": "device",
            "[System]use_docked_mode": "device",
            "[System]language_index": "known",
            "[System]region_index": "known",
            "[System]rng_seed": "noise",
            "[UI]Paths\\language": "known",
            "[UI]Paths\\recentFiles": "noise",
            "[UI]Paths\\gamedirs\\1\\path": "noise",
            "[UI]UILayout\\geometry": "noise",
            "[UI]Shortcuts\\Main%20Window\\Exit\\KeySeq": "noise",
            "[WebService]eden_token": "secret",
            "[WebService]eden_username": "secret",
            "[WebService]web_api_url": "noise",
        },
    ),
    ("retroarch", "cfg"): (
        'savefile_directory = "/fake/saves"\n'
        'cheevos_enable = "true"\n'
        'cheevos_hardcore_mode_enable = "false"\n'
        'cheevos_username = "fake"\n'
        'cheevos_token = "fake"\n'  # gitleaks:allow
        'cheevos_password = "fake"\n'  # gitleaks:allow
        'youtube_stream_key = "fake"\n'  # gitleaks:allow
        'netplay_nickname = "fake"\n'
        'user_language = "12"\n'
        'savestate_auto_save = "true"\n'
        'savestate_auto_load = "true"\n'
        'rewind_enable = "false"\n'
        'run_ahead_enabled = "false"\n'
        'run_ahead_frames = "1"\n'
        'video_refresh_rate = "60.000000"\n'
        'video_fullscreen_x = "0"\n'
        'input_player1_a_btn = "0"\n'
        'input_menu_toggle = "nul"\n'
        'joypad_autoconfig_dir = "/fake/autoconfig"\n'
        'video_window_save_positions = "false"\n'
        'audio_device = "fake"\n'
        'menu_driver = "ozone"\n',
        {
            "savefile_directory": "noise",
            "cheevos_enable": "known",
            "cheevos_hardcore_mode_enable": "known",
            "cheevos_username": "secret",
            "cheevos_token": "secret",
            "cheevos_password": "secret",
            "youtube_stream_key": "secret",
            "netplay_nickname": "known",
            "user_language": "known",
            "savestate_auto_save": "known",
            "savestate_auto_load": "known",
            "rewind_enable": "known",
            "run_ahead_enabled": "device",
            "run_ahead_frames": "device",
            "video_refresh_rate": "device",
            "video_fullscreen_x": "noise",
            "input_player1_a_btn": "noise",
            "input_menu_toggle": "noise",
            "joypad_autoconfig_dir": "noise",
            "video_window_save_positions": "noise",
            "audio_device": "noise",
            "menu_driver": "plain",
        },
    ),
    ("retroarch", "opt"): (
        'reicast_region = "Europe"\n'
        'reicast_language = "Spanish"\n'
        'reicast_per_content_vmus = "VMU A1"\n'
        'reicast_internal_resolution = "1280x960"\n'
        'mupen64plus-43screensize = "320x240"\n'
        'mupen64plus-EnableNativeResFactor = "2"\n'
        'beetle_saturn_shared_int = "enabled"\n',
        {
            "reicast_region": "known",
            "reicast_language": "known",
            "reicast_per_content_vmus": "known",
            "reicast_internal_resolution": "device",
            "mupen64plus-43screensize": "plain",
            "mupen64plus-EnableNativeResFactor": "plain",
            "beetle_saturn_shared_int": "plain",
        },
    ),
    ("duckstation", "settings"): (
        "[Main]\n"
        "SettingsVersion = 3\n"
        "Language = es-ES\n"
        "RunaheadFrameCount = 0\n"
        "[Console]\n"
        "Region = Auto\n"
        "[GPU]\n"
        "ResolutionScale = 3\n"
        "Multisamples = 1\n"
        "PGXPEnable = true\n"
        "TextureFilter = Nearest\n"
        "Adapter = \n"
        "[Cheevos]\n"
        "Enabled = true\n"
        "ChallengeMode = true\n"
        "Username = fake\n"
        "Token = fake\n"  # gitleaks:allow
        "LoginTimestamp = 1\n"
        "[Folders]\n"
        "Cache = /fake/cache\n"
        "[UI]\n"
        "MainWindowGeometry = fake\n"
        "[Hotkeys]\n"
        "FastForward = Keyboard/Tab\n"
        "[Pad1]\n"
        "Type = AnalogController\n",
        {
            "[Main]SettingsVersion": "noise",
            "[Main]Language": "known",
            "[Main]RunaheadFrameCount": "device",
            "[Console]Region": "known",
            "[GPU]ResolutionScale": "device",
            "[GPU]Multisamples": "device",
            "[GPU]PGXPEnable": "known",
            "[GPU]TextureFilter": "known",
            "[GPU]Adapter": "noise",
            "[Cheevos]Enabled": "known",
            "[Cheevos]ChallengeMode": "known",
            "[Cheevos]Username": "secret",
            "[Cheevos]Token": "secret",
            "[Cheevos]LoginTimestamp": "noise",
            "[Folders]Cache": "noise",
            "[UI]MainWindowGeometry": "noise",
            "[Hotkeys]FastForward": "noise",
            "[Pad1]Type": "noise",
        },
    ),
    ("pcsx2", "ini"): (
        "[UI]\n"
        "Language = es-ES\n"
        "SettingsVersion = 1\n"
        "MainWindowGeometry = fake\n"
        "[Folders]\n"
        "Bios = bios\n"
        "SaveStates = sstates\n"
        "SaveStates = sstates\n"
        "[EmuCore]\n"
        "EnableWideScreenPatches = true\n"
        "[EmuCore/GS]\n"
        "Renderer = 14\n"
        "upscale_multiplier = 2\n"
        "MaxAnisotropy = 8\n"
        "[Achievements]\n"
        "Enabled = true\n"
        "ChallengeMode = false\n"
        "Username = fake\n"
        "Token = fake\n"  # gitleaks:allow
        "LoginTimestamp = 1\n"
        "[Pad1]\n"
        "Type = DualShock2\n"
        "[Hotkeys]\n"
        "ToggleFullscreen = Keyboard/Alt & Keyboard/Return\n"
        "[Debugger/CPU/EE]\n"
        "Foo = bar\n",
        {
            "[UI]Language": "known",
            "[UI]SettingsVersion": "noise",
            "[UI]MainWindowGeometry": "noise",
            "[Folders]Bios": "noise",
            "[Folders]SaveStates": "noise",
            "[EmuCore]EnableWideScreenPatches": "known",
            "[EmuCore/GS]Renderer": "known",
            "[EmuCore/GS]upscale_multiplier": "device",
            "[EmuCore/GS]MaxAnisotropy": "device",
            "[Achievements]Enabled": "known",
            "[Achievements]ChallengeMode": "known",
            "[Achievements]Username": "secret",
            "[Achievements]Token": "secret",
            "[Achievements]LoginTimestamp": "noise",
            "[Pad1]Type": "noise",
            "[Hotkeys]ToggleFullscreen": "noise",
            "[Debugger/CPU/EE]Foo": "noise",
        },
    ),
    ("dolphin", "dolphin"): (
        "[General]\n"
        "ISOPath0 = /fake/roms\n"
        "RecursiveISOPaths = True\n"
        "[Interface]\n"
        "LanguageCode = es\n"
        "ThemeName = Clean\n"
        "[Core]\n"
        "SelectedLanguage = 2\n"
        "FallbackRegion = PAL\n"
        "OverrideRegionSettings = True\n"
        "GFXBackend = Vulkan\n"
        "SIDevice0 = 6\n"
        "WiimoteSource0 = 1\n"
        "CPUThread = True\n"
        "[Analytics]\n"
        "ID = fake\n"
        "[Display]\n"
        "Fullscreen = False\n",
        {
            "[General]ISOPath0": "noise",
            "[General]RecursiveISOPaths": "noise",
            "[Interface]LanguageCode": "known",
            "[Interface]ThemeName": "plain",
            "[Core]SelectedLanguage": "known",
            "[Core]FallbackRegion": "known",
            "[Core]OverrideRegionSettings": "known",
            "[Core]GFXBackend": "known",
            "[Core]SIDevice0": "noise",
            "[Core]WiimoteSource0": "noise",
            "[Core]CPUThread": "plain",
            "[Analytics]ID": "noise",
            "[Display]Fullscreen": "noise",
        },
    ),
    ("dolphin", "gfx"): (
        "[Settings]\n"
        "InternalResolution = 2\n"
        "MSAA = 0x00000001\n"
        "SSAA = False\n"
        "ShaderCompilationMode = 2\n"
        "wideScreenHack = False\n"
        "AspectRatio = 0\n"
        "ShowFPS = False\n"
        "[Enhancements]\n"
        "MaxAnisotropy = 2\n",
        {
            "[Settings]InternalResolution": "device",
            "[Settings]MSAA": "device",
            "[Settings]SSAA": "device",
            "[Settings]ShaderCompilationMode": "known",
            "[Settings]wideScreenHack": "known",
            "[Settings]AspectRatio": "known",
            "[Settings]ShowFPS": "plain",
            "[Enhancements]MaxAnisotropy": "device",
        },
    ),
    ("azahar", "qt-config"): (
        "[Renderer]\n"
        "resolution_factor=2\n"
        "resolution_factor\\default=false\n"
        "graphics_api=2\n"
        "use_disk_shader_cache=true\n"
        "physical_device=0\n"
        "[Layout]\n"
        "layout_option=1\n"
        "swap_screen=false\n"
        "large_screen_proportion=4\n"
        "small_screen_position=0\n"
        "custom_top_x=0\n"
        "screen_gap=0\n"
        "[System]\n"
        "region_value=-1\n"
        "init_time=946681277\n"
        "[Controls]\n"
        "profile=0\n"
        "[UI]\n"
        "Paths\\gamedirs\\1\\path=/fake/roms\n"
        "Paths\\language=es\n"
        "Shortcuts\\Main%20Window\\Exit\\KeySeq=Ctrl+Q\n"
        "[WebService]\n"
        "citra_token=fake\n"  # gitleaks:allow
        "citra_username=fake\n"
        "web_api_url=https://fake.invalid\n",
        {
            "[Renderer]resolution_factor": "device",
            "[Renderer]graphics_api": "known",
            "[Renderer]use_disk_shader_cache": "known",
            "[Renderer]physical_device": "noise",
            "[Layout]layout_option": "device",
            "[Layout]swap_screen": "device",
            "[Layout]large_screen_proportion": "device",
            "[Layout]small_screen_position": "device",
            "[Layout]custom_top_x": "device",
            "[Layout]screen_gap": "device",
            "[System]region_value": "known",
            "[System]init_time": "noise",
            "[Controls]profile": "noise",
            "[UI]Paths\\gamedirs\\1\\path": "noise",
            "[UI]Paths\\language": "noise",
            "[UI]Shortcuts\\Main%20Window\\Exit\\KeySeq": "noise",
            "[WebService]citra_token": "secret",
            "[WebService]citra_username": "secret",
            "[WebService]web_api_url": "noise",
        },
    ),
    ("melonds", "config"): (
        'LastBIOSFolder = "/fake/bios"\n'
        "[3D]\n"
        "Renderer = 1\n"
        "[3D.GL]\n"
        "ScaleFactor = 4\n"
        "[Instance0.Firmware]\n"
        'Username = "Fake"\n'
        "Language = 1\n"
        "BirthdayMonth = 3\n"
        "BirthdayDay = 4\n"
        "FavouriteColour = 5\n"
        'Message = ""\n'
        "OverrideSettings = true\n"
        'MAC = "00:00:00:00:00:00"\n'
        "[Instance0.Window0]\n"
        "ScreenLayout = 2\n"
        "ScreenSwap = false\n"
        "IntegerScaling = false\n"
        "[Instance0.Keyboard]\n"
        "A = 45\n"
        "[Instance0.Joystick]\n"
        "A = 0\n"
        "[DS]\n"
        'BIOS9Path = ""\n'
        "[Mic]\n"
        "InputType = 1\n"
        "[Audio]\n"
        "Interpolation = 0\n",
        {
            "LastBIOSFolder": "noise",
            "3D.Renderer": "device",
            "3D.GL.ScaleFactor": "device",
            "Instance0.Firmware.Username": "known",
            "Instance0.Firmware.Language": "known",
            "Instance0.Firmware.BirthdayMonth": "known",
            "Instance0.Firmware.BirthdayDay": "known",
            "Instance0.Firmware.FavouriteColour": "known",
            "Instance0.Firmware.Message": "known",
            "Instance0.Firmware.OverrideSettings": "known",
            "Instance0.Firmware.MAC": "device",
            "Instance0.Window0.ScreenLayout": "device",
            "Instance0.Window0.ScreenSwap": "device",
            "Instance0.Window0.IntegerScaling": "device",
            "Instance0.Keyboard.A": "noise",
            "Instance0.Joystick.A": "noise",
            "DS.BIOS9Path": "noise",
            "Mic.InputType": "noise",
            "Audio.Interpolation": "plain",
        },
    ),
    ("ppsspp", "ini"): (
        "﻿[General]\n"
        "FirstRun = False\n"
        "RunCount = 3\n"
        "CurrentDirectory = /fake/roms\n"
        "Language = es_ES\n"
        "[Recent]\n"
        "FileName0 = /fake/game.iso\n"
        "[Graphics]\n"
        "InternalResolution = 2\n"
        "AnisotropyLevel = 4\n"
        "GraphicsBackend = 3 (VULKAN)\n"
        "VulkanDevice = fake\n"
        "FrameSkip = 0\n"
        "[SystemParam]\n"
        "NickName = Fake\n"
        "Language = 5\n"
        "ButtonPreference = 1\n"
        "MacAddress = 00:00:00:00:00:00\n"
        "[Achievements]\n"
        "AchievementsEnable = True\n"
        "AchievementsChallengeMode = True\n"
        "AchievementsUserName = fake\n"
        "[Network]\n"
        "InfrastructureUsername = fake\n"
        "[DisplayLayout.Portrait]\n"
        "DisplayOffsetX = 0.5\n"
        "[Control]\n"
        "DisableDpadDiagonals = False\n",
        {
            "[General]FirstRun": "noise",
            "[General]RunCount": "noise",
            "[General]CurrentDirectory": "noise",
            "[General]Language": "known",
            "[Recent]FileName0": "noise",
            "[Graphics]InternalResolution": "device",
            "[Graphics]AnisotropyLevel": "device",
            "[Graphics]GraphicsBackend": "known",
            "[Graphics]VulkanDevice": "noise",
            "[Graphics]FrameSkip": "plain",
            "[SystemParam]NickName": "known",
            "[SystemParam]Language": "known",
            "[SystemParam]ButtonPreference": "known",
            "[SystemParam]MacAddress": "noise",
            "[Achievements]AchievementsEnable": "known",
            "[Achievements]AchievementsChallengeMode": "known",
            "[Achievements]AchievementsUserName": "secret",
            "[Network]InfrastructureUsername": "secret",
            "[DisplayLayout.Portrait]DisplayOffsetX": "device",
            "[Control]DisableDpadDiagonals": "noise",
        },
    ),
    ("vita3k", "config"): (
        "---\n"
        "pref-path: /fake/Vita3K/\n"
        "sys-lang: 5\n"
        "sys-button: 1\n"
        "sys-date-format: 1\n"
        "sys-time-format: 1\n"
        "user-id: '00'\n"
        "user-lang: 5\n"
        "backend-renderer: Vulkan\n"
        "resolution-multiplier: 1\n"
        "anisotropic-filtering: 1\n"
        "screen-filter: Bilinear\n"
        "gpu-idx: 0\n"
        "keyboard-button-up: 82\n"
        "controller-binds: [1, 2]\n"
        "initial-setup: true\n"
        "log-level: 0\n"
        "audio-volume: 100\n",
        {
            "pref-path": "noise",
            "sys-lang": "known",
            "sys-button": "known",
            "sys-date-format": "known",
            "sys-time-format": "known",
            "user-id": "known",
            "user-lang": "known",
            "backend-renderer": "known",
            "resolution-multiplier": "device",
            "anisotropic-filtering": "device",
            "screen-filter": "device",
            "gpu-idx": "noise",
            "keyboard-button-up": "noise",
            "controller-binds": "noise",
            "initial-setup": "noise",
            "log-level": "noise",
            "audio-volume": "plain",
        },
    ),
    ("xemu", "config"): (
        "[general]\n"
        "show_welcome = false\n"
        "last_viewed_menu_index = 2\n"
        "[display.quality]\n"
        "surface_scale = 1\n"
        "[sys]\n"
        "mem_limit = '64'\n"
        "[sys.files]\n"
        "hdd_path = '/fake/xbox_hdd.qcow2'\n"
        "[input.bindings]\n"
        "port1 = 'fake'\n",
        {
            "general.show_welcome": "noise",
            "general.last_viewed_menu_index": "noise",
            "display.quality.surface_scale": "device",
            "sys.mem_limit": "known",
            "sys.files.hdd_path": "noise",
            "input.bindings.port1": "noise",
        },
    ),
    ("xenia", "config"): (
        # Canary writes a comment after every value.
        "[Display]\n"
        "fullscreen = false                \t# Whether to launch in fullscreen.\n"
        "[GPU]\n"
        "draw_resolution_scale_x = 1       \t# Integer pixel width scale.\n"
        'gpu = "any"                       \t# Graphics system.\n'
        "[Logging]\n"
        "log_level = 2                     \t# Maximum level to be logged.\n"
        "[Profiles]\n"
        'logged_profile_slot_0_xuid = "B13EBABEBABEBABE"\t# XUID of the profile to load.\n'
        "[Storage]\n"
        'content_root = ""                 \t# Root path for guest content storage.\n'
        "[UI]\n"
        "window_size_x = 1280              \t# Xenia window width\n"
        "[Vulkan]\n"
        "vulkan_device = -1                \t# Index of the preferred Vulkan device.\n",
        {
            "Display.fullscreen": "device",
            "GPU.draw_resolution_scale_x": "device",
            "GPU.gpu": "plain",
            "Logging.log_level": "noise",
            "Profiles.logged_profile_slot_0_xuid": "known",
            "Storage.content_root": "device",
            "UI.window_size_x": "noise",
            "Vulkan.vulkan_device": "device",
        },
    ),
    ("ryujinx", "config"): (
        "{\n"
        '  "version": 70,\n'
        '  "system_language": "AmericanEnglish",\n'
        '  "system_region": "USA",\n'
        '  "system_time_zone": "UTC",\n'
        '  "language_code": "en_US",\n'
        '  "graphics_backend": "Vulkan",\n'
        '  "enable_vsync": true,\n'
        '  "docked_mode": false,\n'
        '  "res_scale": 1,\n'
        '  "max_anisotropy": -1,\n'
        '  "preferred_gpu": "fake",\n'
        '  "multiplayer_ldn_passphrase": "fake",\n'  # gitleaks:allow
        '  "hotkeys": {"toggle_vsync": "F1"},\n'
        '  "window_startup": {"window_size_width": 1280},\n'
        '  "enable_ptc": true\n'
        "}\n",
        {
            "version": "noise",
            "system_language": "known",
            "system_region": "known",
            "system_time_zone": "known",
            "language_code": "known",
            "graphics_backend": "known",
            "enable_vsync": "known",
            "docked_mode": "device",
            "res_scale": "device",
            "max_anisotropy": "device",
            "preferred_gpu": "noise",
            "multiplayer_ldn_passphrase": "secret",
            "hotkeys.toggle_vsync": "noise",
            "window_startup.window_size_width": "noise",
            "enable_ptc": "plain",
        },
    ),
    ("shadps4", "config"): (
        SHADPS4_CONFIG,
        {
            "General.install_dirs": "noise",
            "General.addon_install_dir": "noise",
            "General.home_dir": "noise",
            "General.volume_slider": "plain",
            "General.console_language": "known",
            "General.big_picture_scale": "noise",
            "Input.cursor_state": "noise",
            "GPU.window_width": "device",
            "GPU.full_screen": "device",
            "GPU.fsr_enabled": "plain",
            "Vulkan.gpu_id": "device",
        },
    ),
    ("shadps4", "launcher"): (
        SHADPS4_LAUNCHER,
        {
            "[general_settings]guiLanguage": "known",
            "[general_settings]shadPath": "noise",
            "[general_settings]theme": "plain",
            "[main_window]geometry": "noise",
            "[version_manager]versionSelected": "noise",
        },
    ),
}


def categories(file: SettingsFile, key: str) -> set[str]:
    found = {
        cat for cat in CATEGORIES if any(match(pattern, key) for pattern in getattr(file, cat))
    }
    if not found:
        found.add("plain")
    return found


class ShippedSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = load_profile("steamos-emudeck")
        cls.settings = cls.profile.settings

    def test_thirteen_emulators(self) -> None:
        self.assertEqual(
            sorted(self.settings),
            sorted(
                [
                    "eden",
                    "retroarch",
                    "duckstation",
                    "pcsx2",
                    "dolphin",
                    "azahar",
                    "melonds",
                    "ppsspp",
                    "vita3k",
                    "xemu",
                    "ryujinx",
                    "xenia",
                    "shadps4",
                ]
            ),
        )

    def test_files_match_the_design_table(self) -> None:
        declared = {
            (emu, fid): (f.path, f.format)
            for emu, entry in self.settings.items()
            for fid, f in entry.files.items()
        }
        self.assertEqual(declared, EXPECTED_FILES)

    def test_every_emulator_names_a_confirmed_process(self) -> None:
        for emu, entry in self.settings.items():
            with self.subTest(emulator=emu):
                self.assertFalse(entry.verify)
                self.assertTrue(entry.process)

    def test_process_names(self) -> None:
        self.assertEqual(self.settings["eden"].process, ("eden", "Eden.AppImage"))
        self.assertEqual(
            self.settings["duckstation"].process, ("duckstation-qt", "DuckStation.AppImage")
        )
        self.assertEqual(self.settings["pcsx2"].process, ("pcsx2-qt", "pcsx2-Qt.AppImage"))
        self.assertEqual(
            self.settings["azahar"].process, ("azahar", "azahar-qt", "azahar.AppImage")
        )
        self.assertEqual(self.settings["ppsspp"].process, ("PPSSPPSDL", "PPSSPPQt"))
        # The Proton build: the kernel cuts the name to "xenia_canary.ex", which matches.
        self.assertEqual(self.settings["xenia"].process, ("xenia_canary.exe",))

    def test_shadps4_uses_the_process_names_of_its_profile_table(self) -> None:
        self.assertEqual(self.settings["shadps4"].process, self.profile.shadps4.process)

    def test_retroarch_core_options_are_a_template(self) -> None:
        opt = self.settings["retroarch"].files["opt"]
        self.assertTrue(opt.templated)
        self.assertTrue(opt.create)
        self.assertFalse(self.settings["retroarch"].files["cfg"].templated)

    def test_system_data_files(self) -> None:
        azahar = self.settings["azahar"].data["sysdata-config"]
        self.assertTrue(azahar.path.startswith("~/Emulation/storage/azahar/nand/data/"))
        self.assertTrue(azahar.path.endswith("/sysdata/00010017/00000000/config"))
        self.assertEqual(azahar.create_when, "~/Emulation/storage/azahar/nand")
        # The synced Vita user is user 00, so both its file and its folder are fixed to it.
        vita = self.settings["vita3k"].data["user-00"]
        self.assertEqual(vita.path, "~/Emulation/storage/Vita3K/ux0/user/00/user.xml")
        self.assertEqual(vita.create_when, "~/Emulation/storage/Vita3K/ux0/user/00")
        xemu = self.settings["xemu"].data["eeprom"]
        self.assertEqual(xemu.path, "~/Emulation/storage/xemu/eeprom.bin")
        self.assertEqual(xemu.create_when, "~/Emulation/storage/xemu")
        # Canary keeps the console language and country here, not in its TOML.
        xenia = self.settings["xenia"].data["xconfig"]
        self.assertEqual(xenia.path, "~/Emulation/roms/xbox360/xconfig.settings")
        self.assertEqual(xenia.create_when, "~/Emulation/roms/xbox360")
        data = {(emu, did) for emu, entry in self.settings.items() for did in entry.data}
        self.assertEqual(
            data,
            {
                ("azahar", "sysdata-config"),
                ("vita3k", "user-00"),
                ("xemu", "eeprom"),
                ("xenia", "xconfig"),
            },
        )

    def test_login_only_files_are_not_declared(self) -> None:
        for entry in self.settings.values():
            for f in entry.files.values():
                self.assertNotIn("RetroAchievements", f.path)
                self.assertNotIn("achievements", f.path.lower())

    def test_patterns_are_well_formed(self) -> None:
        for emu, entry in self.settings.items():
            for fid, f in entry.files.items():
                for cat in CATEGORIES:
                    patterns = getattr(f, cat)
                    with self.subTest(emulator=emu, file=fid, list=cat):
                        self.assertEqual(len(patterns), len(set(patterns)), "duplicate pattern")
                        for pattern in patterns:
                            self.assertEqual(pattern, pattern.strip())
                            self.assertTrue(pattern)

    def test_no_secret_pattern_is_also_shared(self) -> None:
        for emu, entry in self.settings.items():
            for fid, f in entry.files.items():
                with self.subTest(emulator=emu, file=fid):
                    self.assertFalse(set(f.secret) & set(f.known))
                    self.assertFalse(set(f.secret) & set(f.device))
                    self.assertFalse(set(f.known) & set(f.device))
                    self.assertFalse(set(f.known) & set(f.noise))

    def test_every_file_has_a_fixture(self) -> None:
        self.assertEqual(set(FIXTURES), set(EXPECTED_FILES))


class ClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = load_profile("steamos-emudeck").settings

    def test_fixture_keys_are_read_in_canonical_form(self) -> None:
        for (emu, fid), (text, expected) in FIXTURES.items():
            f = self.settings[emu].files[fid]
            with self.subTest(emulator=emu, file=fid):
                read = handler(f.format).read(text)
                readable = set(read.values) | set(read.fixed)
                self.assertEqual(readable - set(expected), set(), "key without an expectation")
                missing = set(expected) - readable
                self.assertEqual(missing, set(), "expected key not read from the fixture")

    def test_lists_classify_the_fixture_keys(self) -> None:
        for (emu, fid), (_, expected) in FIXTURES.items():
            f = self.settings[emu].files[fid]
            for key, want in expected.items():
                with self.subTest(emulator=emu, file=fid, key=key):
                    want_set = {want} if isinstance(want, str) else set(want)
                    self.assertEqual(categories(f, key), want_set)

    def test_secrets_are_never_device_or_known(self) -> None:
        for (emu, fid), (_, expected) in FIXTURES.items():
            f = self.settings[emu].files[fid]
            for key in expected:
                cats = categories(f, key)
                with self.subTest(emulator=emu, file=fid, key=key):
                    if "secret" in cats or is_builtin_secret(key):
                        self.assertEqual(cats - {"noise"}, {"secret"})

    def test_login_names_are_secret_in_every_file_that_has_them(self) -> None:
        for emu, fid, key in (
            ("eden", "qt-config", "[WebService]eden_username"),
            ("azahar", "qt-config", "[WebService]citra_username"),
            ("retroarch", "cfg", "cheevos_username"),
            ("duckstation", "settings", "[Cheevos]Username"),
            ("pcsx2", "ini", "[Achievements]Username"),
            ("ppsspp", "ini", "[Achievements]AchievementsUserName"),
        ):
            with self.subTest(emulator=emu, key=key):
                self.assertEqual(categories(self.settings[emu].files[fid], key), {"secret"})

    def test_melonds_firmware_username_is_the_shared_ds_nickname(self) -> None:
        f = self.settings["melonds"].files["config"]
        self.assertEqual(f.secret, ())
        self.assertEqual(categories(f, "Instance0.Firmware.Username"), {"known"})

    def test_dolphin_languages(self) -> None:
        self.assertIn("[Core]SelectedLanguage", self.settings["dolphin"].files["dolphin"].known)
        self.assertIn("[Interface]LanguageCode", self.settings["dolphin"].files["dolphin"].known)

    def test_flycast_per_game_vmus_option_is_shared(self) -> None:
        self.assertIn("reicast_per_content_vmus", self.settings["retroarch"].files["opt"].known)

    def test_retroarch_stream_keys_are_secret_by_one_pattern(self) -> None:
        cfg = self.settings["retroarch"].files["cfg"]
        for key in ("facebook_stream_key", "twitch_stream_key", "youtube_stream_key"):
            self.assertEqual(categories(cfg, key), {"secret"})
        self.assertEqual([p for p in cfg.secret if "stream_key" in p], ["*_stream_key"])

    def test_per_device_keys_the_trial_found(self) -> None:
        eden = self.settings["eden"].files["qt-config"]
        for key in (
            "[System]device_name",
            "[System]use_docked_mode",
            "[Renderer]resolution_setup",
            "[Renderer]max_anisotropy",
            "[Renderer]fullscreen_mode",
        ):
            self.assertIn("device", categories(eden, key))
        self.assertEqual(
            categories(self.settings["ryujinx"].files["config"], "preferred_gpu"), {"noise"}
        )
        self.assertIn(
            "device",
            categories(self.settings["azahar"].files["qt-config"], "[Layout]layout_option"),
        )


class Shadps4SettingsTests(unittest.TestCase):
    """The language keys that are shared, written on a sample of shadPS4's own files."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = load_profile("steamos-emudeck").settings["shadps4"].files

    def test_the_live_config_is_json_and_the_stale_toml_is_not_declared(self) -> None:
        self.assertEqual(self.files["config"].path, "~/.local/share/shadPS4/config.json")
        self.assertNotIn(".toml", "".join(f.path for f in self.files.values()))

    def test_only_the_languages_are_known(self) -> None:
        self.assertEqual(self.files["config"].known, ("General.console_language",))
        self.assertEqual(self.files["launcher"].known, ("[general_settings]guiLanguage",))

    def test_paths_warpzone_sets_are_never_shared(self) -> None:
        for key in ("General.install_dirs", "General.addon_install_dir"):
            self.assertTrue(any(match(p, key) for p in self.files["config"].noise), key)

    def test_the_console_language_is_written_as_an_int_and_nothing_else_changes(self) -> None:
        fmt = handler("json")
        written = fmt.write(SHADPS4_CONFIG, {"General.console_language": 3})
        self.assertEqual(
            written, SHADPS4_CONFIG.replace('"console_language": 1', '"console_language": 3')
        )
        self.assertEqual(fmt.read(written).values["General.console_language"], 3)

    def test_the_gui_language_is_written_as_text_and_nothing_else_changes(self) -> None:
        fmt = handler("ini")
        written = fmt.write(SHADPS4_LAUNCHER, {"[general_settings]guiLanguage": "es_ES"})
        self.assertEqual(
            written, SHADPS4_LAUNCHER.replace("guiLanguage=en_US", "guiLanguage=es_ES")
        )


class HandlerBehaviourTests(unittest.TestCase):
    """Facts about the files that the data relies on."""

    def test_pcsx2_duplicate_folders_key_is_not_writable(self) -> None:
        text, _ = FIXTURES[("pcsx2", "ini")]
        read = handler("ini").read(text)
        self.assertIn("[Folders]SaveStates", read.fixed)
        self.assertNotIn("[Folders]SaveStates", read.values)

    def test_qt_default_companions_are_not_keys(self) -> None:
        for emu in ("eden", "azahar"):
            text, expected = FIXTURES[(emu, "qt-config")]
            read = handler("qt-ini").read(text)
            self.assertFalse([k for k in read.values if k.endswith("\\default")])
            self.assertIn("\\default=false", text)
            self.assertTrue(expected)

    def test_writing_a_qt_key_clears_its_companion(self) -> None:
        text, _ = FIXTURES[("eden", "qt-config")]
        out = handler("qt-ini").write(text, {"[Renderer]resolution_setup": "3"})
        self.assertIn("resolution_setup=3\n", out)
        self.assertIn("resolution_setup\\default=false\n", out)

    def test_ppsspp_bom_is_kept(self) -> None:
        text, _ = FIXTURES[("ppsspp", "ini")]
        out = handler("ini").write(text, {"[General]Language": "en_US"})
        self.assertTrue(out.startswith("﻿[General]"))
        self.assertIn("Language = en_US\n", out)

    def test_vita3k_user_id_is_read_as_written(self) -> None:
        text, _ = FIXTURES[("vita3k", "config")]
        read = handler("yaml-flat").read(text)
        self.assertEqual(read.values["user-id"], "'00'")
        self.assertIn("controller-binds", read.fixed)


if __name__ == "__main__":
    unittest.main()
