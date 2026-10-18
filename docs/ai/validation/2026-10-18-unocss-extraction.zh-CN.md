# UnoCSS 编译后脚本误识别修复

2026-10-18 的 [stage-web 构建](https://github.com/cIeanwater/airi/actions/runs/39136748136) 在 LightningCSS 压缩阶段失败：

```text
Pseudo-elements like '::before' or '::after' can't be followed by selectors like 'SquareBracketBlock'
```

触发文件是 `history.vue?vue&type=script&setup=true&lang.ts`，不是原始 Vue 模板。
Vue 编译后的比较表达式包含 `<`。
Attributify 把它识别为 HTML 标签，再把后续 `scrollbar` patch flag 与消息上下文弹窗的 `open:` 属性拼接。
Scrollbar preset 随后生成非法选择器：

```css
[scrollbar~="open\:"]::-webkit-scrollbar-thumb[open]
```

`scrollbar` 属性来自上游代码。
新增消息上下文弹窗改变了编译后字段的排列，触发既有提取器的误识别。
逐个提取仓库的原始 Vue、TypeScript 文件没有产生上述非法选择器。
真实 `stage-web build` 和最小 Vite 构建都能复现相同错误。

`uno.config.ts` 现在仅排除 Vue 编译后的 script 子请求。
原始 SFC、独立 TypeScript 文件及既有属性语法继续参与提取。
既有 `node_modules` 排除项保持不变。
CSS 压缩仍使用 LightningCSS，没有修改依赖或锁文件。

回归用例在 `.github/scripts/uno-pipeline.test.ts`。
它通过真实 Vite、UnoCSS 和 LightningCSS 构建精简编译结果。
它还检查原始模板的 padding、scrollbar、open 状态，以及独立 TypeScript 中的字体类。
修复前，该用例在 LightningCSS 阶段报相同错误。

本机验证：

- `pnpm exec vitest run --project github-scripts .github/scripts/uno-pipeline.test.ts`：1/1 通过。
- `pnpm -F @proj-airi/stage-web build`：通过，4,990 个模块完成构建。
- 构建仍报告既有 web font 网络获取、WASM 路径和 sourcemap 警告，退出码为 0。

配置依据：[UnoCSS 的构建流水线提取配置](https://unocss.dev/guide/extracting#extracting-from-build-tools-pipeline)。
实现依据是当前安装的 `@unocss/preset-attributify` 66.7.5 提取器，以及 `@unocss/vite` 66.7.5 的预处理扫描。
