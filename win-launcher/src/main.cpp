#include <windows.h>
#include <commctrl.h>
#include <shellapi.h>
#include <urlmon.h>

#include <chrono>
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <functional>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#pragma comment(lib, "urlmon.lib")
#pragma comment(lib, "comctl32.lib")

namespace fs = std::filesystem;

namespace {

constexpr UINT WMU_BOOTSTRAP_STATUS = WM_APP + 1;
constexpr UINT WMU_BOOTSTRAP_LOG = WM_APP + 2;
constexpr UINT WMU_BOOTSTRAP_FAILED = WM_APP + 3;
constexpr UINT WMU_BOOTSTRAP_LAUNCHED = WM_APP + 4;
constexpr int BOOTSTRAP_PROGRESS_MIN = 0;
constexpr int BOOTSTRAP_PROGRESS_MAX = 7;
constexpr wchar_t kBootstrapWindowClass[] = L"yt_asr_bootstrap_window";

struct UiTextMessage {
    int step = BOOTSTRAP_PROGRESS_MIN;
    std::wstring text;
};

struct BootstrapWindowState {
    HWND status_label = nullptr;
    HWND progress_bar = nullptr;
    HWND log_edit = nullptr;
    HWND close_button = nullptr;
    HWND detail_label = nullptr;
    bool can_close = false;
    fs::path log_file;
};

struct BootstrapConfig {
    std::wstring python_version = L"3.13.12";
    std::wstring python_series = L"3.13";
    std::vector<std::wstring> acceptable_python_series = {L"3.13", L"3.12", L"3.11", L"3.10"};
    std::wstring python_installer_url =
        L"https://www.python.org/ftp/python/3.13.12/python-3.13.12-amd64.exe";
    std::wstring ffmpeg_zip_url =
        L"https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip";
    std::wstring workdata_dir_name = L"workdata";
    std::wstring runtime_dir_name = L"runtime";
    std::wstring python_dir_name = L"python";
    std::wstring ffmpeg_dir_name = L"ffmpeg";
    std::wstring cache_dir_name = L"cache";
    std::wstring logs_dir_name = L"logs";
    std::wstring launcher_schema = L"1";
};

struct BootstrapLayout {
    fs::path app_root;
    fs::path runtime_dir;
    fs::path python_dir;
    fs::path python_base_dir;
    fs::path ffmpeg_dir;
    fs::path ffmpeg_bin_dir;
    fs::path cache_dir;
    fs::path pip_cache_dir;
    fs::path logs_dir;
    fs::path log_file;
    fs::path state_file;
    fs::path python_version_file;
    fs::path app_deps_stamp_file;
    fs::path ffmpeg_zip_file;
    fs::path python_installer_file;
    fs::path python_installer_log_file;
    fs::path workdata_dir;
    fs::path python_exe;
    fs::path pythonw_exe;
    fs::path python_base_exe;
    fs::path python_basew_exe;
    fs::path ffmpeg_exe;
    fs::path ffprobe_exe;
};

struct WorkerContext {
    BootstrapConfig config;
    BootstrapLayout layout;
    std::vector<std::wstring> extra_args;
    HWND hwnd = nullptr;
    int exit_code = 1;
};

struct RunningProcess {
    HANDLE process = nullptr;
    HANDLE thread = nullptr;
};

std::wstring wide_from_utf8(const std::string& value) {
    if (value.empty()) {
        return L"";
    }
    const int length = MultiByteToWideChar(CP_UTF8, 0, value.c_str(), -1, nullptr, 0);
    if (length <= 0) {
        throw std::runtime_error("Failed to convert UTF-8 to UTF-16.");
    }
    std::wstring result(static_cast<size_t>(length), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.c_str(), -1, result.data(), length);
    if (!result.empty() && result.back() == L'\0') {
        result.pop_back();
    }
    return result;
}

std::string utf8_from_wide(const std::wstring& value) {
    if (value.empty()) {
        return "";
    }
    const int length = WideCharToMultiByte(CP_UTF8, 0, value.c_str(), -1, nullptr, 0, nullptr, nullptr);
    if (length <= 0) {
        throw std::runtime_error("Failed to convert UTF-16 to UTF-8.");
    }
    std::string result(static_cast<size_t>(length), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value.c_str(), -1, result.data(), length, nullptr, nullptr);
    if (!result.empty() && result.back() == '\0') {
        result.pop_back();
    }
    return result;
}

std::wstring read_text_file_utf8(const fs::path& path) {
    std::ifstream stream(path, std::ios::binary);
    if (!stream) {
        return L"";
    }
    std::ostringstream buffer;
    buffer << stream.rdbuf();
    return wide_from_utf8(buffer.str());
}

void write_text_file_utf8(const fs::path& path, const std::string& content) {
    fs::create_directories(path.parent_path());
    std::ofstream stream(path, std::ios::binary | std::ios::trunc);
    if (!stream) {
        throw std::runtime_error("Failed to open file for writing.");
    }
    stream.write(content.data(), static_cast<std::streamsize>(content.size()));
}

std::wstring trim_copy(std::wstring value) {
    const auto is_space = [](wchar_t ch) { return ch == L' ' || ch == L'\t' || ch == L'\r' || ch == L'\n'; };
    while (!value.empty() && is_space(value.front())) {
        value.erase(value.begin());
    }
    while (!value.empty() && is_space(value.back())) {
        value.pop_back();
    }
    return value;
}

std::wstring sanitize_windows_string(std::wstring value) {
    if (const size_t nul = value.find(L'\0'); nul != std::wstring::npos) {
        value.resize(nul);
    }
    return trim_copy(std::move(value));
}

std::wstring replace_all(std::wstring text, std::wstring_view from, std::wstring_view to) {
    size_t position = 0;
    while ((position = text.find(from, position)) != std::wstring::npos) {
        text.replace(position, from.size(), to);
        position += to.size();
    }
    return text;
}

std::wstring json_escape(const std::wstring& value) {
    std::wstring escaped;
    escaped.reserve(value.size() + 16);
    for (const wchar_t ch : value) {
        switch (ch) {
        case L'\\':
            escaped += L"\\\\";
            break;
        case L'"':
            escaped += L"\\\"";
            break;
        case L'\r':
            escaped += L"\\r";
            break;
        case L'\n':
            escaped += L"\\n";
            break;
        case L'\t':
            escaped += L"\\t";
            break;
        default:
            escaped += ch;
            break;
        }
    }
    return escaped;
}

std::wstring current_timestamp_utc() {
    SYSTEMTIME st{};
    GetSystemTime(&st);
    wchar_t buffer[64]{};
    swprintf_s(
        buffer,
        L"%04u-%02u-%02uT%02u:%02u:%02uZ",
        static_cast<unsigned>(st.wYear),
        static_cast<unsigned>(st.wMonth),
        static_cast<unsigned>(st.wDay),
        static_cast<unsigned>(st.wHour),
        static_cast<unsigned>(st.wMinute),
        static_cast<unsigned>(st.wSecond)
    );
    return buffer;
}

std::wstring last_error_message(DWORD error) {
    LPWSTR buffer = nullptr;
    const DWORD size = FormatMessageW(
        FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr,
        error,
        MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
        reinterpret_cast<LPWSTR>(&buffer),
        0,
        nullptr
    );
    std::wstring result = size && buffer ? buffer : L"Unknown Windows error.";
    if (buffer) {
        LocalFree(buffer);
    }
    return trim_copy(result);
}

std::runtime_error make_error(const std::wstring& message) {
    return std::runtime_error(utf8_from_wide(message));
}

void fail_if(bool condition, const std::wstring& message) {
    if (condition) {
        throw make_error(message);
    }
}

fs::path executable_path() {
    std::wstring buffer(MAX_PATH, L'\0');
    while (true) {
        const DWORD written = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
        fail_if(written == 0, L"Could not determine the launcher path: " + last_error_message(GetLastError()));
        if (written < buffer.size() - 1) {
            buffer.resize(written);
            return fs::path(buffer);
        }
        buffer.resize(buffer.size() * 2);
    }
}

bool looks_like_app_root(const fs::path& root) {
    return fs::exists(root / "pyproject.toml")
        && fs::exists(root / "yt_subtitle_extract" / "gui.py");
}

fs::path find_app_root(const fs::path& exe_path) {
    fs::path current = exe_path.parent_path();
    for (int depth = 0; depth < 8; ++depth) {
        if (looks_like_app_root(current)) {
            return current;
        }
        if (!current.has_parent_path()) {
            break;
        }
        current = current.parent_path();
    }
    return exe_path.parent_path();
}

BootstrapLayout make_layout(const BootstrapConfig& config, const fs::path& app_root) {
    BootstrapLayout layout{};
    layout.app_root = app_root;
    layout.runtime_dir = app_root / config.runtime_dir_name;
    layout.python_dir = layout.runtime_dir / config.python_dir_name;
    layout.python_base_dir = layout.runtime_dir / "python-base";
    layout.ffmpeg_dir = layout.runtime_dir / config.ffmpeg_dir_name;
    layout.ffmpeg_bin_dir = layout.ffmpeg_dir / "bin";
    layout.cache_dir = layout.runtime_dir / config.cache_dir_name;
    layout.pip_cache_dir = layout.cache_dir / "pip";
    layout.logs_dir = layout.runtime_dir / config.logs_dir_name;
    layout.log_file = layout.logs_dir / "bootstrap.log";
    layout.state_file = layout.runtime_dir / "state.json";
    layout.python_version_file = layout.runtime_dir / "python-version.txt";
    layout.app_deps_stamp_file = layout.runtime_dir / "app-deps.stamp";
    layout.python_installer_file = layout.cache_dir / (L"python-" + config.python_version + L"-amd64.exe");
    layout.python_installer_log_file = layout.logs_dir / "python-installer.log";
    layout.ffmpeg_zip_file = layout.cache_dir / "ffmpeg-release-essentials.zip";
    layout.workdata_dir = app_root / config.workdata_dir_name;
    layout.python_exe = layout.python_dir / "Scripts" / "python.exe";
    layout.pythonw_exe = layout.python_dir / "Scripts" / "pythonw.exe";
    layout.python_base_exe = layout.python_base_dir / "python.exe";
    layout.python_basew_exe = layout.python_base_dir / "pythonw.exe";
    layout.ffmpeg_exe = layout.ffmpeg_bin_dir / "ffmpeg.exe";
    layout.ffprobe_exe = layout.ffmpeg_bin_dir / "ffprobe.exe";
    return layout;
}

void append_log_line(const fs::path& log_file, const std::wstring& line) {
    fs::create_directories(log_file.parent_path());
    std::ofstream stream(log_file, std::ios::binary | std::ios::app);
    if (!stream) {
        return;
    }
    const std::string utf8 = utf8_from_wide(current_timestamp_utc() + L"  " + line + L"\r\n");
    stream.write(utf8.data(), static_cast<std::streamsize>(utf8.size()));
}

UiTextMessage* make_ui_text_message(int step, const std::wstring& text) {
    auto* message = new UiTextMessage();
    message->step = step;
    message->text = text;
    return message;
}

void post_status(HWND hwnd, int step, const std::wstring& text) {
    PostMessageW(hwnd, WMU_BOOTSTRAP_STATUS, 0, reinterpret_cast<LPARAM>(make_ui_text_message(step, text)));
}

void post_log(HWND hwnd, const std::wstring& text) {
    PostMessageW(hwnd, WMU_BOOTSTRAP_LOG, 0, reinterpret_cast<LPARAM>(make_ui_text_message(BOOTSTRAP_PROGRESS_MIN, text)));
}

void post_failure(HWND hwnd, const std::wstring& text) {
    PostMessageW(hwnd, WMU_BOOTSTRAP_FAILED, 0, reinterpret_cast<LPARAM>(make_ui_text_message(BOOTSTRAP_PROGRESS_MIN, text)));
}

void append_edit_line(HWND edit, const std::wstring& text) {
    const int length = GetWindowTextLengthW(edit);
    SendMessageW(edit, EM_SETSEL, static_cast<WPARAM>(length), static_cast<LPARAM>(length));
    std::wstring line = text + L"\r\n";
    SendMessageW(edit, EM_REPLACESEL, FALSE, reinterpret_cast<LPARAM>(line.c_str()));
    SendMessageW(edit, EM_SCROLLCARET, 0, 0);
}

void layout_bootstrap_window(const BootstrapWindowState& state, const RECT& rc) {
    constexpr int margin = 16;
    constexpr int button_width = 100;
    constexpr int button_height = 30;
    constexpr int progress_height = 22;
    constexpr int status_height = 44;
    constexpr int detail_height = 36;

    const int width = rc.right - rc.left;
    const int height = rc.bottom - rc.top;
    const int content_width = std::max(100, width - margin * 2);
    const int close_x = width - margin - button_width;
    const int close_y = height - margin - button_height;
    const int log_top = margin + status_height + 8 + progress_height + 8 + detail_height + 8;
    const int log_height = std::max(120, close_y - 8 - log_top);

    MoveWindow(state.status_label, margin, margin, content_width, status_height, TRUE);
    MoveWindow(state.progress_bar, margin, margin + status_height + 8, content_width, progress_height, TRUE);
    MoveWindow(state.detail_label, margin, margin + status_height + 8 + progress_height + 8, content_width, detail_height, TRUE);
    MoveWindow(state.log_edit, margin, log_top, content_width, log_height, TRUE);
    MoveWindow(state.close_button, close_x, close_y, button_width, button_height, TRUE);
}

LRESULT CALLBACK bootstrap_window_proc(HWND hwnd, UINT message, WPARAM w_param, LPARAM l_param) {
    auto* state = reinterpret_cast<BootstrapWindowState*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));

    switch (message) {
    case WM_NCCREATE: {
        auto* create = reinterpret_cast<CREATESTRUCTW*>(l_param);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(create->lpCreateParams));
        return TRUE;
    }
    case WM_CREATE: {
        state = reinterpret_cast<BootstrapWindowState*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        const HFONT font = static_cast<HFONT>(GetStockObject(DEFAULT_GUI_FONT));

        state->status_label = CreateWindowExW(
            0, WC_STATICW, L"Preparing local yt-asr runtime...",
            WS_CHILD | WS_VISIBLE,
            0, 0, 0, 0,
            hwnd, nullptr, nullptr, nullptr
        );
        state->progress_bar = CreateWindowExW(
            0, PROGRESS_CLASSW, nullptr,
            WS_CHILD | WS_VISIBLE,
            0, 0, 0, 0,
            hwnd, nullptr, nullptr, nullptr
        );
        state->detail_label = CreateWindowExW(
            0, WC_STATICW, state->log_file.wstring().c_str(),
            WS_CHILD | WS_VISIBLE,
            0, 0, 0, 0,
            hwnd, nullptr, nullptr, nullptr
        );
        state->log_edit = CreateWindowExW(
            WS_EX_CLIENTEDGE, WC_EDITW, nullptr,
            WS_CHILD | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL | ES_READONLY,
            0, 0, 0, 0,
            hwnd, nullptr, nullptr, nullptr
        );
        state->close_button = CreateWindowExW(
            0, WC_BUTTONW, L"Close",
            WS_CHILD | WS_VISIBLE | WS_DISABLED | BS_PUSHBUTTON,
            0, 0, 0, 0,
            hwnd, reinterpret_cast<HMENU>(1001), nullptr, nullptr
        );

        SendMessageW(state->status_label, WM_SETFONT, reinterpret_cast<WPARAM>(font), TRUE);
        SendMessageW(state->detail_label, WM_SETFONT, reinterpret_cast<WPARAM>(font), TRUE);
        SendMessageW(state->log_edit, WM_SETFONT, reinterpret_cast<WPARAM>(font), TRUE);
        SendMessageW(state->close_button, WM_SETFONT, reinterpret_cast<WPARAM>(font), TRUE);

        SendMessageW(state->progress_bar, PBM_SETRANGE32, BOOTSTRAP_PROGRESS_MIN, BOOTSTRAP_PROGRESS_MAX);
        SendMessageW(state->progress_bar, PBM_SETPOS, BOOTSTRAP_PROGRESS_MIN, 0);

        append_edit_line(state->log_edit, L"Launcher status window ready.");
        append_edit_line(state->log_edit, L"Detailed bootstrap log: " + state->log_file.wstring());
        return 0;
    }
    case WM_SIZE:
        if (state) {
            RECT rc{};
            GetClientRect(hwnd, &rc);
            layout_bootstrap_window(*state, rc);
        }
        return 0;
    case WM_COMMAND:
        if (LOWORD(w_param) == 1001 && state && state->can_close) {
            DestroyWindow(hwnd);
            return 0;
        }
        break;
    case WM_CLOSE:
        if (state && !state->can_close) {
            MessageBoxW(
                hwnd,
                L"Startup is still running. Please wait until the launcher finishes preparing yt-asr.",
                L"yt-asr Launcher",
                MB_OK | MB_ICONINFORMATION
            );
            return 0;
        }
        DestroyWindow(hwnd);
        return 0;
    case WMU_BOOTSTRAP_STATUS: {
        auto* payload = reinterpret_cast<UiTextMessage*>(l_param);
        if (state && payload) {
            SetWindowTextW(state->status_label, payload->text.c_str());
            SendMessageW(state->progress_bar, PBM_SETPOS, payload->step, 0);
            append_edit_line(state->log_edit, payload->text);
        }
        delete payload;
        return 0;
    }
    case WMU_BOOTSTRAP_LOG: {
        auto* payload = reinterpret_cast<UiTextMessage*>(l_param);
        if (state && payload) {
            append_edit_line(state->log_edit, payload->text);
        }
        delete payload;
        return 0;
    }
    case WMU_BOOTSTRAP_FAILED: {
        auto* payload = reinterpret_cast<UiTextMessage*>(l_param);
        if (state && payload) {
            state->can_close = true;
            SetWindowTextW(state->status_label, payload->text.c_str());
            append_edit_line(state->log_edit, L"ERROR: " + payload->text);
            EnableWindow(state->close_button, TRUE);
            SetFocus(state->close_button);
        }
        delete payload;
        return 0;
    }
    case WMU_BOOTSTRAP_LAUNCHED:
        if (state) {
            state->can_close = true;
        }
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    case WM_NCDESTROY:
        delete state;
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
        return 0;
    default:
        break;
    }

    return DefWindowProcW(hwnd, message, w_param, l_param);
}

void ensure_common_controls() {
    INITCOMMONCONTROLSEX icex{};
    icex.dwSize = sizeof(icex);
    icex.dwICC = ICC_STANDARD_CLASSES | ICC_PROGRESS_CLASS;
    InitCommonControlsEx(&icex);
}

HWND create_bootstrap_window(const BootstrapLayout& layout) {
    ensure_common_controls();
    HINSTANCE instance = GetModuleHandleW(nullptr);

    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.lpfnWndProc = bootstrap_window_proc;
    wc.hInstance = instance;
    wc.lpszClassName = kBootstrapWindowClass;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);

    RegisterClassExW(&wc);

    auto* state = new BootstrapWindowState();
    state->log_file = layout.log_file;

    const HWND hwnd = CreateWindowExW(
        0,
        kBootstrapWindowClass,
        L"yt-asr Setup",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
        CW_USEDEFAULT,
        CW_USEDEFAULT,
        760,
        520,
        nullptr,
        nullptr,
        instance,
        state
    );
    fail_if(hwnd == nullptr, L"Could not create the launcher status window.");

    ShowWindow(hwnd, SW_SHOW);
    UpdateWindow(hwnd);
    return hwnd;
}

std::wstring quote_arg(const std::wstring& arg) {
    if (arg.empty()) {
        return L"\"\"";
    }
    const bool needs_quotes = arg.find_first_of(L" \t\"") != std::wstring::npos;
    if (!needs_quotes) {
        return arg;
    }
    std::wstring result = L"\"";
    size_t backslashes = 0;
    for (const wchar_t ch : arg) {
        if (ch == L'\\') {
            ++backslashes;
            continue;
        }
        if (ch == L'"') {
            result.append(backslashes * 2 + 1, L'\\');
            result += L'"';
            backslashes = 0;
            continue;
        }
        if (backslashes > 0) {
            result.append(backslashes, L'\\');
            backslashes = 0;
        }
        result += ch;
    }
    if (backslashes > 0) {
        result.append(backslashes * 2, L'\\');
    }
    result += L'"';
    return result;
}

std::wstring join_command_line(const fs::path& executable, const std::vector<std::wstring>& arguments) {
    std::wstring command = quote_arg(executable.wstring());
    for (const auto& arg : arguments) {
        command += L" ";
        command += quote_arg(arg);
    }
    return command;
}

HANDLE open_log_handle(const fs::path& path) {
    fs::create_directories(path.parent_path());
    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    const HANDLE handle = CreateFileW(
        path.c_str(),
        FILE_APPEND_DATA,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        &sa,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        nullptr
    );
    fail_if(handle == INVALID_HANDLE_VALUE, L"Could not open the bootstrap log file: " + path.wstring());
    return handle;
}

RunningProcess start_process(
    const fs::path& executable,
    const std::vector<std::wstring>& arguments,
    const std::optional<fs::path>& working_directory,
    const fs::path& log_file,
    bool create_no_window
) {
    HANDLE log_handle = open_log_handle(log_file);
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    HANDLE stdin_handle = GetStdHandle(STD_INPUT_HANDLE);
    if (stdin_handle == INVALID_HANDLE_VALUE) {
        stdin_handle = nullptr;
    }
    si.hStdInput = stdin_handle;
    si.hStdOutput = log_handle;
    si.hStdError = log_handle;

    PROCESS_INFORMATION pi{};
    std::wstring command_line = join_command_line(executable, arguments);
    std::vector<wchar_t> mutable_command(command_line.begin(), command_line.end());
    mutable_command.push_back(L'\0');

    DWORD creation_flags = CREATE_UNICODE_ENVIRONMENT;
    if (create_no_window) {
        creation_flags |= CREATE_NO_WINDOW;
    }

    append_log_line(log_file, L"Running: " + executable.wstring());
    const bool search_path = executable.is_relative() && !executable.has_parent_path();
    const LPCWSTR application_name = search_path ? nullptr : executable.c_str();
    const BOOL ok = CreateProcessW(
        application_name,
        mutable_command.data(),
        nullptr,
        nullptr,
        TRUE,
        creation_flags,
        nullptr,
        working_directory ? working_directory->c_str() : nullptr,
        &si,
        &pi
    );
    CloseHandle(log_handle);
    fail_if(!ok, L"Failed to launch " + executable.wstring() + L": " + last_error_message(GetLastError()));

    RunningProcess result{};
    result.process = pi.hProcess;
    result.thread = pi.hThread;
    return result;
}

int wait_for_process(RunningProcess process) {
    if (process.thread) {
        CloseHandle(process.thread);
        process.thread = nullptr;
    }
    WaitForSingleObject(process.process, INFINITE);
    DWORD exit_code = 0;
    GetExitCodeProcess(process.process, &exit_code);
    CloseHandle(process.process);
    return static_cast<int>(exit_code);
}

int run_process(
    const fs::path& executable,
    const std::vector<std::wstring>& arguments,
    const std::optional<fs::path>& working_directory,
    const fs::path& log_file,
    bool create_no_window
) {
    return wait_for_process(start_process(executable, arguments, working_directory, log_file, create_no_window));
}

void ensure_directories(const BootstrapLayout& layout) {
    fs::create_directories(layout.runtime_dir);
    fs::create_directories(layout.cache_dir);
    fs::create_directories(layout.logs_dir);
    fs::create_directories(layout.workdata_dir);
    fs::create_directories(layout.ffmpeg_bin_dir);
    fs::create_directories(layout.pip_cache_dir);
}

void download_file(const std::wstring& url, const fs::path& destination, const fs::path& log_file) {
    fs::create_directories(destination.parent_path());
    const fs::path temp_path = destination;
    append_log_line(log_file, L"Downloading: " + url + L" -> " + destination.wstring());
    HRESULT hr = URLDownloadToFileW(nullptr, url.c_str(), temp_path.c_str(), 0, nullptr);
    if (FAILED(hr)) {
        throw make_error(
            L"Download failed for " + url + L" (HRESULT 0x" +
            wide_from_utf8([](HRESULT value) {
                std::ostringstream stream;
                stream << std::hex << std::uppercase << static_cast<unsigned long>(value);
                return stream.str();
            }(hr)) + L")"
        );
    }
}

std::wstring relative_ps_path(const fs::path& path) {
    return replace_all(path.wstring(), L"'", L"''");
}

void expand_archive_with_powershell(const fs::path& zip_path, const fs::path& destination, const fs::path& log_file) {
    if (fs::exists(destination)) {
        fs::remove_all(destination);
    }
    fs::create_directories(destination);
    const std::wstring script =
        L"Expand-Archive -LiteralPath '" + relative_ps_path(zip_path) +
        L"' -DestinationPath '" + relative_ps_path(destination) + L"' -Force";
    const int code = run_process(
        L"powershell.exe",
        {
            L"-NoProfile",
            L"-ExecutionPolicy", L"Bypass",
            L"-Command", script
        },
        std::nullopt,
        log_file,
        true
    );
    if (code != 0) {
        throw make_error(L"Failed to extract archive: " + zip_path.wstring());
    }
}

std::optional<fs::path> find_file_recursive(const fs::path& root, const std::wstring& filename) {
    if (!fs::exists(root)) {
        return std::nullopt;
    }
    for (const auto& entry : fs::recursive_directory_iterator(root)) {
        if (!entry.is_regular_file()) {
            continue;
        }
        if (_wcsicmp(entry.path().filename().c_str(), filename.c_str()) == 0) {
            return entry.path();
        }
    }
    return std::nullopt;
}

std::optional<std::wstring> read_registry_default_string(HKEY hive, const std::wstring& subkey) {
    DWORD type = 0;
    DWORD size = 0;
    const LSTATUS size_status = RegGetValueW(
        hive,
        subkey.c_str(),
        nullptr,
        RRF_RT_REG_SZ,
        &type,
        nullptr,
        &size
    );
    if (size_status != ERROR_SUCCESS || size == 0) {
        return std::nullopt;
    }

    std::wstring value(size / sizeof(wchar_t), L'\0');
    const LSTATUS read_status = RegGetValueW(
        hive,
        subkey.c_str(),
        nullptr,
        RRF_RT_REG_SZ,
        &type,
        value.data(),
        &size
    );
    if (read_status != ERROR_SUCCESS) {
        return std::nullopt;
    }
    return sanitize_windows_string(std::move(value));
}

std::optional<std::wstring> read_registry_named_string(HKEY hive, const std::wstring& subkey, const std::wstring& name) {
    DWORD type = 0;
    DWORD size = 0;
    const LSTATUS size_status = RegGetValueW(
        hive,
        subkey.c_str(),
        name.c_str(),
        RRF_RT_REG_SZ,
        &type,
        nullptr,
        &size
    );
    if (size_status != ERROR_SUCCESS || size == 0) {
        return std::nullopt;
    }

    std::wstring value(size / sizeof(wchar_t), L'\0');
    const LSTATUS read_status = RegGetValueW(
        hive,
        subkey.c_str(),
        name.c_str(),
        RRF_RT_REG_SZ,
        &type,
        value.data(),
        &size
    );
    if (read_status != ERROR_SUCCESS) {
        return std::nullopt;
    }
    return sanitize_windows_string(std::move(value));
}

std::optional<fs::path> normalize_python_executable_candidate(const std::wstring& raw_value) {
    std::wstring cleaned = sanitize_windows_string(raw_value);
    if (cleaned.empty()) {
        return std::nullopt;
    }

    fs::path candidate = fs::path(cleaned);
    std::wstring filename = candidate.filename().wstring();
    std::wstring lowered = filename;
    for (auto& ch : lowered) {
        ch = static_cast<wchar_t>(towlower(ch));
    }

    if (lowered.empty() || lowered == L"." || lowered == L".." || lowered.find(L".exe") == std::wstring::npos) {
        candidate /= L"python.exe";
    }
    return candidate;
}

fs::path ensure_python_executable_path(fs::path candidate) {
    std::wstring filename = candidate.filename().wstring();
    std::wstring lowered = filename;
    for (auto& ch : lowered) {
        ch = static_cast<wchar_t>(towlower(ch));
    }
    if (lowered.empty() || lowered == L"." || lowered == L".." || lowered.find(L".exe") == std::wstring::npos) {
        candidate /= L"python.exe";
    }
    return candidate;
}

bool python_candidate_supports_gui(const fs::path& python_exe, const fs::path& log_file) {
    const fs::path normalized = ensure_python_executable_path(python_exe);
    if (!fs::exists(normalized)) {
        return false;
    }
    const int code = run_process(
        normalized,
        {
            L"-c",
            L"import sys, tkinter; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
        },
        std::nullopt,
        log_file,
        true
    );
    return code == 0;
}

std::optional<fs::path> detect_registered_python_base_for_series(
    const std::wstring& python_series,
    const fs::path& log_file
) {
    const std::wstring subkey = L"Software\\Python\\PythonCore\\" + python_series + L"\\InstallPath";
    const std::vector<HKEY> hives = {HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE};
    for (const HKEY hive : hives) {
        const auto exec_value = read_registry_named_string(hive, subkey, L"ExecutablePath");
        const auto win_exec_value = read_registry_named_string(hive, subkey, L"WindowedExecutablePath");
        if (exec_value && !exec_value->empty()) {
            const auto python_exe = normalize_python_executable_candidate(*exec_value);
            const auto pythonw_exe = win_exec_value && !win_exec_value->empty()
                ? normalize_python_executable_candidate(*win_exec_value)
                : std::optional<fs::path>(python_exe ? python_exe->parent_path() / "pythonw.exe" : fs::path{});
            if (python_exe && pythonw_exe && fs::exists(*python_exe) && fs::exists(*pythonw_exe)) {
                append_log_line(log_file, L"Probing registered Python " + python_series + L" at " + python_exe->wstring());
                if (python_candidate_supports_gui(*python_exe, log_file)) {
                    return *python_exe;
                }
                append_log_line(log_file, L"Skipping " + python_exe->wstring() + L" because tkinter is not available.");
            }
        }
        const auto value = read_registry_default_string(hive, subkey);
        if (!value || value->empty()) {
            continue;
        }
        const auto python_exe = normalize_python_executable_candidate(*value);
        if (!python_exe) {
            continue;
        }
        const fs::path pythonw_exe = python_exe->parent_path() / "pythonw.exe";
        if (fs::exists(*python_exe) && fs::exists(pythonw_exe)) {
            append_log_line(log_file, L"Probing registered Python " + python_series + L" at " + python_exe->wstring());
            if (python_candidate_supports_gui(*python_exe, log_file)) {
                return *python_exe;
            }
            append_log_line(log_file, L"Skipping " + python_exe->wstring() + L" because tkinter is not available.");
        }
    }
    return std::nullopt;
}

std::optional<fs::path> detect_python_from_path(const fs::path& log_file) {
    DWORD required = SearchPathW(nullptr, L"python.exe", nullptr, 0, nullptr, nullptr);
    if (required == 0) {
        return std::nullopt;
    }
    std::wstring buffer(required, L'\0');
    DWORD written = SearchPathW(nullptr, L"python.exe", nullptr, required, buffer.data(), nullptr);
    if (written == 0 || written >= required) {
        return std::nullopt;
    }
    if (!buffer.empty() && buffer.back() == L'\0') {
        buffer.pop_back();
    }
    fs::path candidate = ensure_python_executable_path(fs::path(sanitize_windows_string(std::move(buffer))));
    if (!fs::exists(candidate)) {
        return std::nullopt;
    }
    append_log_line(log_file, L"Probing PATH Python at " + candidate.wstring());
    if (python_candidate_supports_gui(candidate, log_file)) {
        return candidate;
    }
    append_log_line(log_file, L"Skipping PATH Python " + candidate.wstring() + L" because tkinter is not available.");
    return std::nullopt;
}

std::optional<fs::path> detect_registered_python_base(const BootstrapConfig& config, const fs::path& log_file) {
    for (const auto& series : config.acceptable_python_series) {
        if (const auto candidate = detect_registered_python_base_for_series(series, log_file)) {
            return candidate;
        }
    }
    return detect_python_from_path(log_file);
}

void create_local_python_env(const fs::path& base_python_exe_raw, const BootstrapLayout& layout) {
    const fs::path base_python_exe = ensure_python_executable_path(base_python_exe_raw);
    const fs::path base_pythonw_exe = base_python_exe.parent_path() / "pythonw.exe";
    fail_if(
        !fs::exists(base_python_exe),
        L"The detected base Python executable does not exist: " + base_python_exe.wstring()
    );
    fail_if(
        !fs::exists(base_pythonw_exe),
        L"The detected base Python windowed executable does not exist: " + base_pythonw_exe.wstring()
    );
    append_log_line(layout.log_file, L"Creating local Python environment from " + base_python_exe.wstring());
    if (fs::exists(layout.python_dir)) {
        fs::remove_all(layout.python_dir);
    }
    const int code = run_process(
        base_python_exe,
        {
            L"-m", L"venv",
            layout.python_dir.wstring(),
            L"--clear"
        },
        std::nullopt,
        layout.log_file,
        true
    );
    if (code != 0) {
        throw make_error(
            L"Failed to create the local Python environment from "
            + base_python_exe.wstring()
            + L" (exit code "
            + wide_from_utf8(std::to_string(code))
            + L")."
        );
    }
    fail_if(!fs::exists(layout.python_exe), L"The local Python environment was created but python.exe was not found in " + layout.python_dir.wstring());
    fail_if(!fs::exists(layout.pythonw_exe), L"The local Python environment was created but pythonw.exe was not found in " + layout.python_dir.wstring());
}

void ensure_ffmpeg(const BootstrapConfig& config, const BootstrapLayout& layout) {
    if (fs::exists(layout.ffmpeg_exe) && fs::exists(layout.ffprobe_exe)) {
        append_log_line(layout.log_file, L"Local ffmpeg already present.");
        return;
    }

    download_file(config.ffmpeg_zip_url, layout.ffmpeg_zip_file, layout.log_file);
    const fs::path extract_root = layout.cache_dir / "ffmpeg-extract";
    expand_archive_with_powershell(layout.ffmpeg_zip_file, extract_root, layout.log_file);

    const auto ffmpeg_source = find_file_recursive(extract_root, L"ffmpeg.exe");
    const auto ffprobe_source = find_file_recursive(extract_root, L"ffprobe.exe");
    fail_if(!ffmpeg_source || !ffprobe_source, L"Could not locate ffmpeg.exe and ffprobe.exe inside the downloaded archive.");

    fs::create_directories(layout.ffmpeg_bin_dir);
    fs::copy_file(*ffmpeg_source, layout.ffmpeg_exe, fs::copy_options::overwrite_existing);
    fs::copy_file(*ffprobe_source, layout.ffprobe_exe, fs::copy_options::overwrite_existing);
    append_log_line(layout.log_file, L"Installed ffmpeg to " + layout.ffmpeg_bin_dir.wstring());
}

void ensure_python_runtime(const BootstrapConfig& config, const BootstrapLayout& layout) {
    const std::wstring installed_version = trim_copy(read_text_file_utf8(layout.python_version_file));
    const bool python_present = fs::exists(layout.python_exe) && fs::exists(layout.pythonw_exe);
    if (python_present && installed_version == config.python_version && python_candidate_supports_gui(layout.python_exe, layout.log_file)) {
        append_log_line(layout.log_file, L"Local Python " + config.python_version + L" already present.");
        return;
    }
    if (python_present && installed_version == config.python_version) {
        append_log_line(layout.log_file, L"Local Python runtime exists but does not have tkinter. Rebuilding it.");
    }

    if (const auto existing_base = detect_registered_python_base(config, layout.log_file)) {
        append_log_line(
            layout.log_file,
            L"Using detected GUI-capable Python at "
            + existing_base->parent_path().wstring()
            + L" to create the local runtime."
        );
        create_local_python_env(*existing_base, layout);
        fail_if(
            !python_candidate_supports_gui(layout.python_exe, layout.log_file),
            L"The local Python environment was created, but tkinter is still not available in " + layout.python_exe.wstring()
        );
        write_text_file_utf8(layout.python_version_file, utf8_from_wide(config.python_version + L"\n"));
        return;
    }

    append_log_line(layout.log_file, L"Installing local base Python " + config.python_version + L".");
    if (fs::exists(layout.python_base_dir)) {
        fs::remove_all(layout.python_base_dir);
    }
    download_file(config.python_installer_url, layout.python_installer_file, layout.log_file);

    if (fs::exists(layout.python_installer_log_file)) {
        fs::remove(layout.python_installer_log_file);
    }

    const std::vector<std::wstring> args = {
        L"/quiet",
        L"/log", layout.python_installer_log_file.wstring(),
        L"InstallAllUsers=0",
        L"TargetDir=" + layout.python_base_dir.wstring(),
        L"AssociateFiles=0",
        L"Include_launcher=0",
        L"Include_pip=1",
        L"Include_exe=1",
        L"Include_lib=1",
        L"Include_dev=1",
        L"Include_tcltk=1",
        L"Include_test=0",
        L"PrependPath=0",
        L"Shortcuts=0"
    };
    const int code = run_process(layout.python_installer_file, args, std::nullopt, layout.log_file, true);
    if (code != 0) {
        throw make_error(
            L"Python installer failed with exit code "
            + wide_from_utf8(std::to_string(code))
            + L". See "
            + layout.python_installer_log_file.wstring()
        );
    }
    fail_if(!fs::exists(layout.python_base_exe), L"Python was installed but python.exe was not found in " + layout.python_base_dir.wstring());
    fail_if(!fs::exists(layout.python_basew_exe), L"Python was installed but pythonw.exe was not found in " + layout.python_base_dir.wstring());
    append_log_line(layout.log_file, L"Creating the local Python environment from the bundled base runtime.");
    create_local_python_env(layout.python_base_exe, layout);
    fail_if(
        !python_candidate_supports_gui(layout.python_exe, layout.log_file),
        L"The bundled local Python environment was created, but tkinter is not available in " + layout.python_exe.wstring()
    );
    write_text_file_utf8(layout.python_version_file, utf8_from_wide(config.python_version + L"\n"));
}

void set_child_environment(const BootstrapLayout& layout) {
    std::wstring current_path;
    if (const DWORD length = GetEnvironmentVariableW(L"PATH", nullptr, 0); length > 0) {
        current_path.resize(length, L'\0');
        GetEnvironmentVariableW(L"PATH", current_path.data(), length);
        if (!current_path.empty() && current_path.back() == L'\0') {
            current_path.pop_back();
        }
    }

    const fs::path venv_scripts = layout.python_dir / "Scripts";
    std::wstring new_path = layout.ffmpeg_bin_dir.wstring() + L";"
        + venv_scripts.wstring() + L";"
        + layout.python_dir.wstring();
    if (!current_path.empty()) {
        new_path += L";" + current_path;
    }

    SetEnvironmentVariableW(L"PATH", new_path.c_str());
    SetEnvironmentVariableW(L"PYTHONNOUSERSITE", L"1");
    SetEnvironmentVariableW(L"PYTHONUTF8", L"1");
    SetEnvironmentVariableW(L"PIP_DISABLE_PIP_VERSION_CHECK", L"1");
    SetEnvironmentVariableW(L"PIP_CACHE_DIR", layout.pip_cache_dir.wstring().c_str());
}

void run_python(const BootstrapLayout& layout, const std::vector<std::wstring>& arguments, const std::optional<fs::path>& cwd) {
    const int code = run_process(layout.python_exe, arguments, cwd, layout.log_file, true);
    if (code != 0) {
        throw make_error(L"Python command failed with exit code " + wide_from_utf8(std::to_string(code)) + L". See " + layout.log_file.wstring());
    }
}

std::wstring app_dependency_fingerprint(const BootstrapConfig& config, const BootstrapLayout& layout) {
    const fs::path pyproject = layout.app_root / "pyproject.toml";
    fail_if(!fs::exists(pyproject), L"Could not find pyproject.toml in " + layout.app_root.wstring());

    const auto timestamp = fs::last_write_time(pyproject).time_since_epoch().count();
    const auto size = fs::file_size(pyproject);

    std::wstringstream stream;
    stream << L"schema=" << config.launcher_schema << L"\n";
    stream << L"python=" << config.python_version << L"\n";
    stream << L"pyproject_size=" << static_cast<unsigned long long>(size) << L"\n";
    stream << L"pyproject_mtime=" << timestamp << L"\n";
    return stream.str();
}

void ensure_bootstrap_python_tools(const BootstrapLayout& layout) {
    run_python(layout, {L"-m", L"ensurepip", L"--upgrade"}, std::nullopt);
    run_python(
        layout,
        {
            L"-m", L"pip", L"install",
            L"--upgrade",
            L"--disable-pip-version-check",
            L"--no-warn-script-location",
            L"pip",
            L"setuptools",
            L"wheel"
        },
        std::nullopt
    );
}

void ensure_app_installed(const BootstrapConfig& config, const BootstrapLayout& layout) {
    const std::wstring desired_fingerprint = app_dependency_fingerprint(config, layout);
    const std::wstring current_fingerprint = read_text_file_utf8(layout.app_deps_stamp_file);
    if (trim_copy(current_fingerprint) == trim_copy(desired_fingerprint)) {
        append_log_line(layout.log_file, L"Application dependencies are already up to date.");
        return;
    }

    append_log_line(layout.log_file, L"Installing or updating local application dependencies.");
    run_python(
        layout,
        {
            L"-m", L"pip", L"install",
            L"--upgrade",
            L"--disable-pip-version-check",
            L"--no-warn-script-location",
            L"--upgrade-strategy", L"only-if-needed",
            L".[all]"
        },
        layout.app_root
    );
    write_text_file_utf8(layout.app_deps_stamp_file, utf8_from_wide(desired_fingerprint));
}

void update_yt_dlp(const BootstrapLayout& layout) {
    append_log_line(layout.log_file, L"Updating yt-dlp.");
    run_python(
        layout,
        {
            L"-m", L"pip", L"install",
            L"--upgrade",
            L"--disable-pip-version-check",
            L"--no-warn-script-location",
            L"yt-dlp"
        },
        std::nullopt
    );
}

void write_state_file(const BootstrapConfig& config, const BootstrapLayout& layout) {
    std::wostringstream json;
    json << L"{\n";
    json << L"  \"launcher_schema\": \"" << json_escape(config.launcher_schema) << L"\",\n";
    json << L"  \"updated_at_utc\": \"" << json_escape(current_timestamp_utc()) << L"\",\n";
    json << L"  \"app_root\": \"" << json_escape(layout.app_root.wstring()) << L"\",\n";
    json << L"  \"python_version\": \"" << json_escape(config.python_version) << L"\",\n";
    json << L"  \"python_env_path\": \"" << json_escape(layout.python_dir.wstring()) << L"\",\n";
    json << L"  \"python_path\": \"" << json_escape(layout.python_exe.wstring()) << L"\",\n";
    json << L"  \"python_base_path\": \"" << json_escape(layout.python_base_dir.wstring()) << L"\",\n";
    json << L"  \"ffmpeg_path\": \"" << json_escape(layout.ffmpeg_exe.wstring()) << L"\",\n";
    json << L"  \"workdata_path\": \"" << json_escape(layout.workdata_dir.wstring()) << L"\"\n";
    json << L"}\n";
    write_text_file_utf8(layout.state_file, utf8_from_wide(json.str()));
}

std::wstring mutex_name_for_root(const fs::path& root) {
    const auto value = std::hash<std::wstring>{}(root.wstring());
    std::wstringstream stream;
    stream << L"Local\\yt_asr_bootstrap_" << std::hex << value;
    return stream.str();
}

HANDLE acquire_single_instance_mutex(const BootstrapLayout& layout) {
    const HANDLE mutex = CreateMutexW(nullptr, TRUE, mutex_name_for_root(layout.app_root).c_str());
    fail_if(mutex == nullptr, L"Could not create the launcher mutex: " + last_error_message(GetLastError()));
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        CloseHandle(mutex);
        throw make_error(L"yt-asr is already running or bootstrapping from this folder.");
    }
    return mutex;
}

RunningProcess launch_app_process(const BootstrapLayout& layout, const std::vector<std::wstring>& extra_args) {
    std::vector<std::wstring> args = {
        L"-m",
        L"yt_asr",
        L"--workspace",
        layout.workdata_dir.wstring()
    };
    args.insert(args.end(), extra_args.begin(), extra_args.end());
    append_log_line(layout.log_file, L"Launching yt_asr with workspace " + layout.workdata_dir.wstring());
    return start_process(layout.python_exe, args, layout.workdata_dir, layout.log_file, true);
}

DWORD WINAPI bootstrap_thread_proc(LPVOID raw_context) {
    auto* context = static_cast<WorkerContext*>(raw_context);
    HANDLE mutex = nullptr;

    try {
        post_status(context->hwnd, 0, L"Checking launcher state...");
        append_log_line(context->layout.log_file, L"Bootstrap worker thread started.");
        post_log(context->hwnd, L"App root: " + context->layout.app_root.wstring());
        post_log(context->hwnd, L"Bootstrap log: " + context->layout.log_file.wstring());

        mutex = acquire_single_instance_mutex(context->layout);

        post_status(context->hwnd, 1, L"Preparing local folders...");
        ensure_directories(context->layout);

        post_status(context->hwnd, 2, L"Preparing the local Python runtime...");
        set_child_environment(context->layout);
        ensure_python_runtime(context->config, context->layout);
        set_child_environment(context->layout);

        post_status(context->hwnd, 3, L"Checking local FFmpeg tools...");
        ensure_ffmpeg(context->config, context->layout);

        post_status(context->hwnd, 4, L"Bootstrapping pip and packaging tools...");
        ensure_bootstrap_python_tools(context->layout);

        post_status(context->hwnd, 5, L"Installing yt-asr into the local runtime...");
        ensure_app_installed(context->config, context->layout);

        post_status(context->hwnd, 6, L"Updating yt-dlp...");
        update_yt_dlp(context->layout);

        post_status(context->hwnd, 7, L"Launching yt-asr...");
        write_state_file(context->config, context->layout);
        RunningProcess child = launch_app_process(context->layout, context->extra_args);
        post_log(context->hwnd, L"yt-asr launched. The setup window will now close.");
        PostMessageW(context->hwnd, WMU_BOOTSTRAP_LAUNCHED, 0, 0);

        context->exit_code = wait_for_process(child);
        append_log_line(
            context->layout.log_file,
            L"Application exited with code " + wide_from_utf8(std::to_string(context->exit_code))
        );
    } catch (const std::exception& exc) {
        context->exit_code = 1;
        const std::wstring message = wide_from_utf8(exc.what());
        append_log_line(context->layout.log_file, L"Bootstrap failed: " + message);
        post_failure(context->hwnd, message);
    }

    if (mutex) {
        ReleaseMutex(mutex);
        CloseHandle(mutex);
    }
    return 0;
}

std::vector<std::wstring> collect_extra_args() {
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) {
        return {};
    }
    std::vector<std::wstring> args;
    for (int i = 1; i < argc; ++i) {
        args.emplace_back(argv[i]);
    }
    LocalFree(argv);
    return args;
}

void show_error_box(const std::wstring& message) {
    MessageBoxW(nullptr, message.c_str(), L"yt-asr Launcher", MB_OK | MB_ICONERROR);
}

} // namespace

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    try {
        WorkerContext context{};
        context.config = BootstrapConfig{};
        const fs::path exe_path = executable_path();
        context.layout = make_layout(context.config, find_app_root(exe_path));
        context.extra_args = collect_extra_args();

        ensure_directories(context.layout);
        append_log_line(context.layout.log_file, L"Launcher started from " + exe_path.wstring());

        const HWND hwnd = create_bootstrap_window(context.layout);
        context.hwnd = hwnd;

        const HANDLE worker = CreateThread(
            nullptr,
            0,
            bootstrap_thread_proc,
            &context,
            0,
            nullptr
        );
        fail_if(worker == nullptr, L"Could not create the bootstrap worker thread.");

        MSG msg{};
        while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }

        WaitForSingleObject(worker, INFINITE);
        CloseHandle(worker);
        return context.exit_code;
    } catch (const std::exception& exc) {
        const std::wstring message = wide_from_utf8(exc.what());
        show_error_box(message);
        return 1;
    }
}
