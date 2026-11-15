"""存储模式常量。

单独成模块是为了让各存储后端与 `image_bed` 共用同一份真值表，又不必让
后端反向 import `image_bed`。`image_bed` 仍然 re-export 这些名字，保持
`from image_bed import MODE_CFB` 等旧导入路径可用。
"""

from __future__ import annotations

MODE_LOCAL = "local"
MODE_HTTP = "generic_http"
MODE_R2 = "cloudflare_r2"
MODE_OCI = "oracle_oci"
MODE_CFB = "cloudflare_imgbed"
