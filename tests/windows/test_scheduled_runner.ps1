$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$runner = Join-Path $repoRoot 'scripts\windows\run-scheduled.ps1'
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) "threads-browser-runner-$([guid]::NewGuid())"
$null = New-Item -ItemType Directory -Path $temporaryRoot
$capturePath = Join-Path $temporaryRoot 'arguments.txt'
$fakeUvPath = Join-Path $temporaryRoot 'uv.cmd'
[IO.File]::WriteAllText(
    $fakeUvPath,
    "@echo off`r`n> `"%TB_RUNNER_CAPTURE%`" echo %*`r`nexit /b %TB_RUNNER_EXIT%`r`n"
)

$previousPath = $env:PATH
$previousCapture = $env:TB_RUNNER_CAPTURE
$previousExit = $env:TB_RUNNER_EXIT
try {
    $env:PATH = "$temporaryRoot;$previousPath"
    $env:TB_RUNNER_CAPTURE = $capturePath
    $env:TB_RUNNER_EXIT = '0'
    $powerShell = (Get-Command pwsh -ErrorAction Stop).Source
    & $powerShell -NoProfile -File $runner `
        -Alias offline -MaxPosts 3 -MaxScrolls 2 -MaxTotalPosts 10 -MaxRuntimeSeconds 90
    if ($LASTEXITCODE -ne 0) {
        throw 'The scheduled runner did not propagate the successful wrapper exit code.'
    }

    $captured = [IO.File]::ReadAllText($capturePath).Trim()
    $actual = @(
        [regex]::Matches($captured, '"[^"]*"|[^\s]+') | ForEach-Object {
            $_.Value.Trim('"')
        }
    )
    $expected = @(
        'run', '--project', $repoRoot, 'tb', 'run', 'offline', '--headed',
        '--max-posts', '3', '--max-scrolls', '2', '--max-total-posts', '10',
        '--max-runtime-seconds', '90', '--no-readiness-prompt', '--scheduled'
    )
    if (($actual -join "`n") -cne ($expected -join "`n")) {
        throw 'The scheduled runner forwarded an unexpected command line.'
    }

    $env:TB_RUNNER_EXIT = '23'
    & $powerShell -NoProfile -File $runner -Alias offline
    if ($LASTEXITCODE -ne 23) {
        throw 'The scheduled runner did not propagate the application failure exit code.'
    }
    Write-Output 'Windows scheduled wrapper argument and exit-code smoke passed.'
}
finally {
    $env:PATH = $previousPath
    if ($null -eq $previousCapture) {
        Remove-Item Env:TB_RUNNER_CAPTURE -ErrorAction SilentlyContinue
    }
    else {
        $env:TB_RUNNER_CAPTURE = $previousCapture
    }
    if ($null -eq $previousExit) {
        Remove-Item Env:TB_RUNNER_EXIT -ErrorAction SilentlyContinue
    }
    else {
        $env:TB_RUNNER_EXIT = $previousExit
    }

    $temporaryRoot = [IO.Path]::GetFullPath($temporaryRoot)
    $temporaryPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($temporaryRoot.StartsWith($temporaryPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
