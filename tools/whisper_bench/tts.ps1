# Speak corpus.json with the local zh-CN voices (rotating) into tts\sNN.wav, 16 kHz mono s16.
param([switch]$Force)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $here 'tts'
New-Item -ItemType Directory -Force $out | Out-Null
$lines = Get-Content -Raw -Encoding UTF8 (Join-Path $here 'corpus.json') | ConvertFrom-Json
$voices = @('Microsoft Huihui Desktop', 'Microsoft Kangkang', 'Microsoft Yaoyao')
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
for ($i = 0; $i -lt $lines.Count; $i++) {
    $path = Join-Path $out ('s{0:D2}.wav' -f ($i + 1))
    if ((Test-Path $path) -and -not $Force) { continue }
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $synth.SelectVoice($voices[$i % 3])
    $synth.Rate = 0
    $synth.SetOutputToWaveFile($path, $fmt)
    $synth.Speak($lines[$i])
    $synth.SetOutputToNull()
    $synth.Dispose()
    Write-Output ('{0} {1} {2}' -f $path, $voices[$i % 3], (Get-Item $path).Length)
}
