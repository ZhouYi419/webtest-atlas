[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 3081,
    [string]$DshCliPath,
    [switch]$UseLocalModels,
    [switch]$OpenBrowser
)

$ErrorActionPreference = 'Stop'
$project = Split-Path $PSScriptRoot -Parent
$version = '0.2.2-rc.5'

function Find-DshCli {
    # 按显式脚本、全局安装、npx 缓存的顺序定位已验证版本，避免启动时联网更新。
    param([string]$ExplicitPath, [string]$RequiredVersion)
    if ($ExplicitPath) {
        $candidates = @((Resolve-Path -LiteralPath $ExplicitPath).Path)
    } else {
        $packageRoot = (& npm.cmd root -g).Trim()
        $cache = Join-Path ((& npm.cmd config get cache).Trim()) '_npx'
        $candidates = @(Join-Path $packageRoot '@deepseek-ai/dsh/lib/bin.js')
        if (Test-Path -LiteralPath $cache) {
            $candidates += @(Get-ChildItem -Directory -LiteralPath $cache | ForEach-Object {
                Join-Path $_.FullName 'node_modules/@deepseek-ai/dsh/lib/bin.js'
            })
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $manifest = Join-Path (Split-Path (Split-Path $candidate -Parent) -Parent) 'package.json'
            $package = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($package.name -eq '@deepseek-ai/dsh' -and $package.version -eq $RequiredVersion) {
                return $candidate
            }
        }
    }
    throw "Requires @deepseek-ai/dsh@$RequiredVersion. Install with npm exec --yes --package=@deepseek-ai/dsh@$RequiredVersion -- dsh --version, or supply -DshCliPath to its lib/bin.js."
}

$cli = Find-DshCli $DshCliPath $version
$python = Join-Path $project '.venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Missing .venv/Scripts/python.exe. Set up the project Python environment first.'
}
$local = Join-Path $project '.local'
$workspace = Join-Path $local 'workspace'
$dshHome = Join-Path $local 'dsh'
$profile = Join-Path $dshHome 'profiles/atlas'
$assets = Join-Path $project 'integrations/dsh'
foreach ($dir in $workspace, $profile) {
    [void][System.IO.Directory]::CreateDirectory($dir)
}
Copy-Item -LiteralPath (Join-Path $assets 'package.json') -Destination $profile -Force
$overlay = Join-Path $assets 'atlas.patch.yml'
$overlays = @('--patch', $overlay)
$webArgs = @('--host', '127.0.0.1', '--port', $Port)
if (!$OpenBrowser) {
    $webArgs += '--no-open'
}
$nodeOptions = @()
# 变量只在本次宿主运行期间生效；DSH 与 MCP 分别使用工作区和项目根目录。
$launch = @{
    DSH_HOME = $dshHome
    DSH_CWD = $workspace
    DSH_TOOLS_MODE = 'native'
    DSH_TELEMETRY_DISABLED = '1'
    ATLAS_PROJECT = $project
    ATLAS_PYTHON = $python
}
if ($UseLocalModels) {
    # Atlas 从原 DSH 目录读取模型配置，DSH 官方凭据提供器从该目录解析凭据。
    $modelHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }
    $launch['ATLAS_MODEL_HOME'] = (Resolve-Path -LiteralPath $modelHome).Path
    $launch['ATLAS_DSH_CLI'] = $cli
    $launch['ATLAS_RESTORE_DRIVER_OPTIONS'] = '1'
    $launch['ATLAS_ORIGINAL_NODE_OPTIONS'] = $env:NODE_OPTIONS
    $overlays += @('--patch', (Join-Path $assets 'local-models.patch.yml'))
    # 官方凭据提供器会尝试迁移旧格式，限制宿主写入位置，保护复用的本机模型文件。
    $nodeOptions = @(
        '--permission', '--allow-fs-read=*', "--allow-fs-write=$local",
        "--allow-fs-write=$([IO.Path]::GetTempPath())", '--allow-child-process',
        '--allow-worker', '--allow-addons'
    )
}
$previous = @{}
foreach ($key in $launch.Keys) {
    $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
    [Environment]::SetEnvironmentVariable($key, $launch[$key], 'Process')
}
Push-Location -LiteralPath $workspace
try {
    & node @nodeOptions $cli --profile atlas @overlays @webArgs
    $status = $LASTEXITCODE
} finally {
    Pop-Location
    foreach ($key in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process')
    }
}
exit $status
