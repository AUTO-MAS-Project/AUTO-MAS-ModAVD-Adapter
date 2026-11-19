<!-- auto-mas:i18n
{
  "display_name_i18n": {
    "zh_CN": "ModAVD 模拟器适配",
    "en": "ModAVD Adapter",
    "ja": "ModAVD エミュレーター"
  },
  "description_i18n": {
    "zh_CN": "ModAVD 模拟器生命周期、设置及组件管理",
    "en": "ModAVD device and component management",
    "ja": "ModAVD デバイス管理"
  },
  "tags_i18n": {
    "zh_CN": [
      "模拟器"
    ],
    "en": [
      "emulator"
    ],
    "ja": [
      "エミュレーター"
    ]
  }
}
-->

# ModAVD 模拟器适配

选择 ModAVD 安装后，由其官方 CLI 管理实例、设置、组件检查与更新。

## 安装与发布

发行包为 `auto-mas-mod-avd-adapter`，插件 ID 为 `mod_avd_adapter`。
适用于 Python 3.12 和已接入游戏适配插件的 AUTO-MAS 宿主，要求 `auto-mas-core>=5.9.0b1,<6`。
通过插件市场安装时由宿主提供 core，独立安装时需要提供兼容的 SDK wheel。

首次发布前，在 [PyPI 账户的 Publishing 页面](https://pypi.org/manage/account/publishing/) 登记待定发布者。
项目名填 `auto-mas-mod-avd-adapter`，仓库所有者填 `AUTO-MAS-Project`，仓库名填 `AUTO-MAS-ModAVD-Adapter`，工作流文件名填 `publish.yml`，环境名填 `pypi`。

分支和 PR 会构建并校验 wheel 与源码包。发布时，在默认分支的提交上打与 `pyproject.toml` 版本一致的 `v` 标签并推送；GitHub Actions 通过 Trusted Publishing 上传分发包。
