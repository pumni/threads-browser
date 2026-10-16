param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')]
    [string]$Alias,

    [ValidateRange(1, 20)]
    [int]$MaxPosts = 20,

    [ValidateRange(0, 20)]
    [int]$MaxScrolls = 8,

    [ValidateRange(1, 400)]
    [int]$MaxTotalPosts = 40,

    [ValidateRange(30, 86400)]
    [int]$MaxRuntimeSeconds = 720
)

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$uv = Get-Command uv -ErrorAction Stop
$arguments = @(
    'run', '--project', $repoRoot, 'tb', 'run', $Alias,
    '--headed',
    '--max-posts', $MaxPosts,
    '--max-scrolls', $MaxScrolls,
    '--max-total-posts', $MaxTotalPosts,
    '--max-runtime-seconds', $MaxRuntimeSeconds,
    '--no-readiness-prompt',
    '--scheduled'
)

& $uv.Source @arguments
exit $LASTEXITCODE
