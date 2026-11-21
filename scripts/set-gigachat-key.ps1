$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw 'Create the project .env from .env.example before setting the key.'
}

$secureKey = Read-Host 'GigaChat Authorization key (without Basic)' -AsSecureString
try {
    $key = [System.Net.NetworkCredential]::new('', $secureKey).Password.Trim()
    if ($key -cnotmatch '\A[A-Za-z0-9+/]+={0,2}\z') {
        throw 'Enter the Authorization key from the GigaChat dashboard without the Basic prefix.'
    }

    $content = [System.IO.File]::ReadAllText($envPath)
    # Match whole bindings so a line in another setting's quoted value stays untouched.
    $pattern = @'
(?mx)
^[ \t]*(?:export[ \t]+)?
(?<name>'[^']+'|[^\s=\#'"]+)[ \t]*=[ \t]*
(?<value>'(?:\\'|[^'])*'|"(?:\\"|[^"])*"|[^\r\n]*?)
[ \t]*(?:[ \t]+\#[^\r\n]*)?(?=\r?$)
'@
    $bindings = [regex]::Matches($content, $pattern)
    $credentials = @($bindings | Where-Object { $_.Groups['name'].Value.Trim("'") -ceq 'GIGACHAT_CREDENTIALS' })
    if ($credentials.Count -gt 0) {
        foreach ($binding in ($credentials | Sort-Object -Property Index -Descending)) {
            $value = $binding.Groups['value']
            $content = $content.Remove($value.Index, $value.Length).Insert($value.Index, $key)
        }
    } else {
        $firstNewline = [regex]::Match($content, '\r?\n')
        $newline = if ($firstNewline.Success) { $firstNewline.Value } else { [Environment]::NewLine }
        if ($content.Length -gt 0 -and -not $content.EndsWith("`n")) {
            $content += $newline
        }
        $content += 'GIGACHAT_CREDENTIALS=' + $key + $newline
    }
    [System.IO.File]::WriteAllText($envPath, $content, [System.Text.UTF8Encoding]::new($false))
} finally {
    $secureKey.Dispose()
    Remove-Variable key, content, bindings, credentials, binding, value -ErrorAction SilentlyContinue
}

Write-Output 'GigaChat Authorization key saved to .env.'
