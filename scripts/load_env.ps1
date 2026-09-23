<#
.SYNOPSIS
    Loads KEY=VALUE pairs from the project-root .env into the current process
    environment. Dot-sourced by the launchers, so the server they spawn
    inherits the values.

    Format: one KEY=VALUE per line; blank lines and lines starting with # are
    ignored; an optional leading "export " is accepted; values may be wrapped
    in matching single or double quotes (no escape processing).

    Variables already set in the real environment win over .env, so a one-off
    `set AV1QUEUE_PORT=9000 && runGUI.bat` still overrides the file.

    Returns an array of warning strings for malformed lines (empty if none).
#>

function Import-DotEnv([string]$Path) {
    $warnings = @()
    if (-not (Test-Path -LiteralPath $Path)) { return ,$warnings }

    $lineNo = 0
    foreach ($raw in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $lineNo++
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        if ($line.StartsWith("export ")) { $line = $line.Substring(7).TrimStart() }

        $eq = $line.IndexOf("=")
        if ($eq -lt 1) {
            $warnings += ".env line ${lineNo}: expected KEY=VALUE, ignored"
            continue
        }
        $key = $line.Substring(0, $eq).Trim()
        $value = $line.Substring($eq + 1).Trim()
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            $warnings += ".env line ${lineNo}: invalid name '$key', ignored"
            continue
        }
        if ($value.Length -ge 2 -and (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        if (-not [Environment]::GetEnvironmentVariable($key, "Process")) {
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
    return ,$warnings
}
