[English](documentation-packaging.md) | [简体中文](documentation-packaging_zh.md)

# 平台包中的文档

平台归档从项目主仓与所选组件源码组装文档。源码导航与归档导航使用不同的目录布局。
现有 E2E 组装器复制各 owner 的用例后，调用 `test/e2e/assemble_docs.py` 复制文档并改写
文档链接。该步骤不修改可执行示例、脚本、配置值或组件二进制。

## 目录布局

| 源码位置 | 归档位置 |
| --- | --- |
| 主仓与组件的 `docs/*` | `docs/*`，保留现有扁平入口 |
| 组件 `README.md` / `README_zh.md` | `docs/<component>.md` / `docs/<component>_zh.md` |
| 组件原生构建、示例、贡献及许可证说明 | `docs/<component>/<original-path>` |
| 主仓 `docs/` 与 `test/` 以外的文档 | `docs/project/<original-path>` |
| 主仓 `test/` 文档 | 原有 `test/` 路径 |
| 组件 `test/e2e/` 文档 | `test/e2e/<component>/<original-relative-path>` |

存在两种语言时同时打包。已有的完整英文单语文档仍然有效。许可证和归属声明文件保留原文。
不包含生成的构建输出、依赖/vendor 目录与 Git 元数据。扁平目标路径冲突及文档输入中的
符号链接会被拒绝，不会静默覆盖另一个组件的文档。

## 导航与源码版本

指向已包含文档、图片和许可证文件的链接改为归档中的实际相对路径。组件 README 改名后，
双向语言选择链接随之更新。指向未装入归档的源码文件时，使用相应源码 revision 的 GitHub
链接。围栏代码块和行内代码示例保持不变。处理普通行内链接、引用定义以及 HTML
`href`/`src` 属性；复杂 Markdown 仍需要人工复核。

不带 fragment 的目录链接在包含对应 README 时沿用来源页面的语言，包括回退到组件
README 的组件 `docs/` 链接；没有中文版时回退到英文。直接文件链接和带显式 fragment
的目录链接保留指定文件或默认 README 目标，以维持原有锚点契约。

发布打包器从既有的所选发布清单读取组件引用，并使用聚合版本构造主仓源码链接。
这些引用仅传给文档组装，不改变版本选择。直接从源码组装时，有 Git 元数据则使用 HEAD，
否则源码链接使用 `main`。本地验收可以提供制表符分隔的 `DOCS_SOURCE_REFS` 文件，每行
包含 owner 和精确源码 revision。这是组装元数据，不是运行时配置项。

runtime 与 vmlinux 单元可以选择 guest-runtime 的不同提交。因此发布打包器单独提供
`DOCS_VMLINUX_SOURCE`，从所选 kernel 源码同时取得 `vmlinux.md` 和 `vmlinux_zh.md`。
若所选旧 kernel 没有中文对应文档，不会用 runtime 单元另一 revision 的中文文档替代。

可以识别且指向已包含文档的跨仓 `main` 链接在组装集合内解析。绝对 GitHub `main` 链接
若指向所选源码中存在的其他文件或目录，则与相对源码链接一样固定到该源码的所选引用，
并保留 query 与 fragment。若绝对 `main` URL 指向所选源码中不存在的文件，则保留原样，
需要单独检视。显式指向历史版本的 URL 保留为历史引用。外部链接，包括私有组件的源码 URL，仍需按照
[检视策略](documentation-policy_zh.md)单独检查访问能力。

## 验证

```sh
python3 -m unittest release/test_documentation_package.py
make test-release-tools
```

专项测试覆盖语言选择、原生构建链接、源码 URL、可执行内容不变、跨仓链接、路径冲突、
符号链接和 kernel 语言版本独立选择。发布测试还会解包实际平台 tarball，检查两份 kernel
文档均来自所选 vmlinux 源码。最终验收还必须用实际检视的源码集合执行组装、检查解包产物，
并验证全部相对路径和标题锚点。翻译完整性需要单独进行语义检视；打包检查通过不能证明
尚未完成的文档已翻译。
