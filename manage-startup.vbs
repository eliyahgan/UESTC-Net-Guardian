Option Explicit

Dim shell, fso, projectDir, executablePath, mode, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
executablePath = fso.BuildPath(projectDir, "dist\UESTCNetGuardian\UESTCNetGuardian.exe")

If Not fso.FileExists(executablePath) Then
    WScript.Echo "UESTCNetGuardian.exe is missing. Build it with build_guardian.ps1 first."
    WScript.Quit 1
End If

mode = "enable"
If WScript.Arguments.Count > 0 Then
    If LCase(WScript.Arguments(0)) = "remove" Then
        mode = "disable"
    ElseIf LCase(WScript.Arguments(0)) = "status" Then
        mode = "status"
    End If
End If

command = Chr(34) & executablePath & Chr(34) & " --startup " & mode
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
