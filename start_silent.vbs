' ============================================================
'   桌面宠物 - 静默启动（不弹黑色控制台窗口）
'   双击即可启动，关闭窗口/右键托盘退出
' ============================================================

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)

' 设置 PYTHONPATH 指向项目自带的 .local-packages
env = "PYTHONPATH=" & scriptDir & "\.local-packages;"

WshShell.Environment("Process").Item("PYTHONPATH") = scriptDir & "\.local-packages"
WshShell.CurrentDirectory = scriptDir
WshShell.Run "cmd /c python main.py", 0, False

Set WshShell = Nothing
Set FSO = Nothing