# Software inventory collector for Windows machines (Windows PowerShell 5.1+).
#
# Prints one JSON document on stdout, the same shape as collect_unix.py's;
# `python -m discovery.cli software-import` reads it on the app host.
# scripts/collect-software.sh runs it over ssh as an -EncodedCommand, so it
# must stay one self-contained script. Read-only: it reads the registry's
# Uninstall keys, Store app packages and file timestamps, and changes nothing.
#
# Windows records no publication date for installed programs. The closest
# available stand-in is the timestamp of the program's own executable (from
# DisplayIcon), which installers normally keep from the vendor's build; when
# there is none, the registry's InstallDate is reported, labelled as such.
#
# Deliberately not collected: install paths (they carry the user's profile
# folder) and Windows itself, which is recorded against the hardware asset.

$ErrorActionPreference = 'Continue'   # PS 5.1: native stderr + 'Stop' kills the script
$ProgressPreference = 'SilentlyContinue'

function Get-DateString($value) {
    if ($null -eq $value) { return $null }
    if ($value -is [datetime]) { return $value.ToUniversalTime().ToString('yyyy-MM-dd') }
    $s = "$value".Trim()
    if ($s -match '^(\d{4})(\d{2})(\d{2})$') { return "$($Matches[1])-$($Matches[2])-$($Matches[3])" }
    return $null
}

function Get-ExeDate($displayIcon) {
    if (-not $displayIcon) { return $null }
    $path = ($displayIcon -replace '^"', '' -replace '",.*$', '' -replace '"$', '' -replace ',\s*-?\d+$', '').Trim()
    if ($path -notmatch '\.exe$') { return $null }
    try {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            return Get-DateString (Get-Item -LiteralPath $path).LastWriteTimeUtc
        }
    } catch { }
    return $null
}

$notes = New-Object System.Collections.ArrayList
$packages = New-Object System.Collections.ArrayList
$seen = @{}

$keys = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
foreach ($key in $keys) {
    $entries = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
    foreach ($e in $entries) {
        $name = $e.DisplayName
        if (-not $name) { continue }
        if ($e.SystemComponent -eq 1) { continue }          # hidden OS components
        if ($e.ParentKeyName) { continue }                  # updates to another entry
        if ($e.ReleaseType -match 'Update|Hotfix') { continue }
        if ($name -match '\(KB\d+\)|^(Security )?Update for ') { continue }
        $dedupe = "$name|$($e.DisplayVersion)"
        if ($seen.ContainsKey($dedupe)) { continue }
        $seen[$dedupe] = $true
        $published = Get-ExeDate $e.DisplayIcon
        $installed = Get-DateString $e.InstallDate
        $rec = [ordered]@{
            source           = 'windows'
            name             = "$name"
            version          = if ($e.DisplayVersion) { "$($e.DisplayVersion)" } else { $null }
            publisher        = if ($e.Publisher) { "$($e.Publisher)" } else { $null }
            installed        = $installed
            published        = $published
            published_source = if ($published) { 'program file date (approx.)' } else { $null }
            homepage         = if ($e.URLInfoAbout) { "$($e.URLInfoAbout)" } else { $null }
        }
        [void]$packages.Add($rec)
    }
}

# Microsoft Store apps the user installed (SignatureKind 'Store'); built-in
# ones are 'System' and are part of Windows.
try {
    foreach ($a in (Get-AppxPackage -PackageTypeFilter Main -ErrorAction Stop | Where-Object { $_.SignatureKind -eq 'Store' })) {
        $installed = $null
        try { $installed = Get-DateString (Get-Item -LiteralPath $a.InstallLocation -ErrorAction Stop).CreationTimeUtc } catch { }
        # The manifest's DisplayName is the name people see ("Simple
        # Solitaire"); Microsoft's own often hold an ms-resource: reference
        # instead, which the importer replaces with a tidied package name.
        $display = $null
        try {
            $dn = "$((Get-AppxPackageManifest -Package $a.PackageFullName -ErrorAction Stop).Package.Properties.DisplayName)"
            if ($dn -and $dn -notmatch '^ms-resource:') { $display = $dn }
        } catch { }
        [void]$packages.Add([ordered]@{
            source       = 'appx'
            name         = "$($a.Name)"
            display_name = $display
            version      = "$($a.Version)"
            publisher    = ("$($a.Publisher)" -replace '^CN=([^,]+).*$', '$1')
            installed    = $installed
        })
    }
} catch {
    [void]$notes.Add('Store apps not collected: ' + $_.Exception.Message)
}

# Pending updates, if the WinGet PowerShell module is installed.
if (Get-Command Get-WinGetPackage -ErrorAction SilentlyContinue) {
    try {
        $upgrades = @{}
        foreach ($w in (Get-WinGetPackage -ErrorAction Stop | Where-Object { $_.IsUpdateAvailable })) {
            $upgrades["$($w.Name)"] = "$($w.AvailableVersions[0])"
        }
        foreach ($p in $packages) {
            if ($upgrades.ContainsKey($p.name)) {
                $p['update_available'] = $true
                $p['latest_version'] = $upgrades[$p.name]
            }
        }
    } catch {
        [void]$notes.Add('winget update check failed: ' + $_.Exception.Message)
    }
} else {
    [void]$notes.Add('WinGet PowerShell module not installed; pending updates not checked')
}

$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
$macs = @()
try {
    $macs = @(Get-NetAdapter -Physical -ErrorAction Stop | Where-Object { $_.MacAddress } |
        ForEach-Object { ($_.MacAddress -replace '-', ':').ToLower() })
} catch { }

$doc = [ordered]@{
    collector_version = 1
    collected_at      = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    platform          = 'windows'
    hostname          = "$env:COMPUTERNAME"
    os                = [ordered]@{ name = if ($os) { "$($os.Caption)" } else { 'Windows' }; version = if ($os) { "$($os.Version)" } else { $null } }
    macs              = $macs
    packages          = $packages
    notes             = $notes
}
$json = $doc | ConvertTo-Json -Depth 6 -Compress
# ASCII-only output: escape anything else as \uXXXX so the console code page
# an ssh session happens to get can't mangle names like "Microsoft(R) ...".
$json = [regex]::Replace($json, '[^\x00-\x7F]', { param($m) '\u{0:x4}' -f [int][char]$m.Value })
[Console]::Out.WriteLine($json)
