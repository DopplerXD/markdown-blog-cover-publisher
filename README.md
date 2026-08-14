# Markdown Blog Cover Publisher

独立的 Markdown 博客文章封面生成脚本。脚本不依赖 skill 系统，可放在任意位置运行，并通过文件顶部的硬编码配置连接目标 Git 仓库和图像模型 API。

## 使用

1. 打开 `blog_cover_workflow.py`，填写 `REPO_PATH`、图片输出位置和 API Key（也可以设置 `DASHSCOPE_API_KEY` 环境变量）。
2. 确认目标仓库中的图片目录和本脚本目录已按需加入 Git；脚本默认把临时图片放在 `.cover-tmp/`，该目录不会被脚本提交。
3. 运行：

   ```bash
   python3 blog_cover_workflow.py
   ```

脚本会检索新建且尚未有封面的 Markdown 文件，先批量生成临时封面，再在终端逐篇等待审批：

| 输入 | 操作 |
| --- | --- |
| `1` | 保存、提交并推送 |
| `2` | 保存并提交 |
| `3` | 仅保存 |
| `4` | 重绘，回车后输入新的提示指导 |
| `5` | 跳过当前图片并保留临时图 |
| `6` | 停止脚本并保留此前结果 |
| `7` | 停止并回滚本次已产生的仓库操作 |

长文章正文超过 1000 个 Unicode 字符时，默认先使用 `qwen3.5-flash` 生成不超过 1200 字的摘要；图像模型 `qwen-image-3.0` 只接收标题、摘要和有限的视觉提示，不会接收完整长 Markdown。相关开关和模型配置均在脚本顶部。

## 配置要点

- `REPO_PATH`：目标博客 Git 仓库路径；默认是脚本所在目录。
- `TEMP_IMAGE_DIR`：临时图片目录；默认是脚本目录下的 `.cover-tmp/`。
- `FINAL_IMAGE_ROOT`：正式图片根目录；为空时写入文章目录下的 `assets/<文章标题>/`。
- `QWEN_SUMMARY_API_URL`、`QWEN_SUMMARY_API_KEY`、`QWEN_SUMMARY_MODEL`：摘要模型配置。
- `IMAGE_API_URL`、`IMAGE_API_KEY`、`IMAGE_MODEL`：图像模型配置。

默认请求格式是 DashScope 兼容接口。如使用其他服务商，按脚本中的请求体、响应路径和 headers 配置扩展即可。

## Git 注意事项

初始化仓库后，README 和脚本可按需提交：

```bash
git add README.md blog_cover_workflow.py .gitignore
git commit -m "chore: add markdown blog cover publisher"
```

脚本不会自动修改 `.gitignore`、不会提交自身，也不会提交 `.cover-tmp/`。请根据你的博客仓库布局手动纳入正式图片目录。
