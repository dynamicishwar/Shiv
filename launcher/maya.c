// Maya.exe - launcher for the Shiv avatar assistant.
// Starts D:\Shiv\shiv_avatar.py with the avatar_env Python (no console window).
// If Maya is already running, the app just brings its window forward.
// On each run it registers itself under HKCU "App Paths", so Win+R -> "maya" works.
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shlwapi.h>
#include <wchar.h>

static const wchar_t *DEFAULT_DIR = L"D:\\Shiv";

static BOOL file_exists(const wchar_t *p) {
    DWORD a = GetFileAttributesW(p);
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

static void register_app_path(const wchar_t *exe) {
    HKEY k;
    if (RegCreateKeyExW(HKEY_CURRENT_USER,
            L"Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\Maya.exe",
            0, NULL, 0, KEY_SET_VALUE, NULL, &k, NULL) == ERROR_SUCCESS) {
        RegSetValueExW(k, NULL, 0, REG_SZ, (const BYTE *)exe, (DWORD)((wcslen(exe) + 1) * sizeof(wchar_t)));
        RegCloseKey(k);
    }
}

int WINAPI wWinMain(HINSTANCE h, HINSTANCE p, PWSTR args, int show) {
    wchar_t self[MAX_PATH], dir[MAX_PATH], script[MAX_PATH], py[MAX_PATH];
    GetModuleFileNameW(NULL, self, MAX_PATH);
    register_app_path(self);

    // Use the exe's own folder if Shiv lives there, else D:\Shiv.
    wcscpy(dir, self);
    PathRemoveFileSpecW(dir);
    swprintf(script, MAX_PATH, L"%ls\\shiv_avatar.py", dir);
    if (!file_exists(script)) {
        wcscpy(dir, DEFAULT_DIR);
        swprintf(script, MAX_PATH, L"%ls\\shiv_avatar.py", dir);
    }
    swprintf(py, MAX_PATH, L"%ls\\avatar_env\\Scripts\\pythonw.exe", dir);

    if (!file_exists(script) || !file_exists(py)) {
        wchar_t msg[1024];
        swprintf(msg, 1024,
            L"Couldn't find Maya's files.\n\nExpected:\n%ls\n%ls\n\nRun setup_avatar.bat in the Shiv folder first.",
            script, py);
        MessageBoxW(NULL, msg, L"Maya", MB_ICONERROR | MB_OK);
        return 1;
    }

    wchar_t cmd[4096];
    swprintf(cmd, 4096, L"\"%ls\" \"%ls\" %ls", py, script, args ? args : L"");

    STARTUPINFOW si = { sizeof(si) };
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, dir, &si, &pi)) {
        wchar_t msg[512];
        swprintf(msg, 512, L"Couldn't start Maya (error %lu).", GetLastError());
        MessageBoxW(NULL, msg, L"Maya", MB_ICONERROR | MB_OK);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
