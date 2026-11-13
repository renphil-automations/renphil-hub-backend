[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Snapshot,
    [Parameter(Mandatory=$true)][ValidateSet('local','sandbox','production','unknown')][string]$Environment,
    [Parameter(Mandatory=$true)][string]$ExpectedHost,
    [Parameter(Mandatory=$true)][string]$ExpectedDatabase,
    [string]$Python = 'python',
    [string]$EnvFile,
    [switch]$ApprovedBlankSourceIsHub,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$snapshotPath = (Resolve-Path -LiteralPath $Snapshot).Path
if ($EnvFile) { $EnvFile = (Resolve-Path -LiteralPath $EnvFile).Path } else { $EnvFile = '-' }
if (Test-Path -LiteralPath $Python -PathType Leaf) { $Python = (Resolve-Path -LiteralPath $Python).Path }
$runner = @'
import runpy, sys
env_file, snapshot, environment, expected_host, expected_database, blank_hub, apply = sys.argv[1:]
if env_file != '-':
    from dotenv import load_dotenv
    if not load_dotenv(env_file, override=True):
        raise SystemExit('Environment file did not load; no import performed.')
from app.config import get_settings
from app.db_v2.database import engine_v2
runtime = get_settings().hub_environment
url = engine_v2.url
if url.host != expected_host or url.database != expected_database:
    raise SystemExit('Configured database does not match the reviewed host and database; no import performed.')
if runtime not in {'local', environment}:
    raise SystemExit('Snapshot environment differs from configured Hub environment; no import performed.')
print('Target verified:', url.host, '/', url.database, '; runtime:', runtime, '; snapshot environment:', environment, flush=True)
sys.argv = ['scripts.import_feedback_snapshot', snapshot, '--environment', environment]
if blank_hub == 'yes': sys.argv += ['--missing-source', 'hub']
if apply == 'yes': sys.argv += ['--apply', '--expected-host', expected_host, '--expected-database', expected_database]
runpy.run_module('scripts.import_feedback_snapshot', run_name='__main__')
'@
$blank = if ($ApprovedBlankSourceIsHub) { 'yes' } else { 'no' }
$mode = if ($Apply) { 'yes' } else { 'no' }
Push-Location -LiteralPath $repo
try {
    & $Python -c $runner $EnvFile $snapshotPath $Environment $ExpectedHost $ExpectedDatabase $blank $mode
    if ($LASTEXITCODE -ne 0) { throw "Migration command failed (exit $LASTEXITCODE)." }
} finally { Pop-Location }
