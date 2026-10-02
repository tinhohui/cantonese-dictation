# 粵語 + 英文 離線語音輸入（macOS）

撳住 **右⌘** 講嘢，鬆手就喺游標位置出字。廣東話、英文、中英夾雜都得，
自動加標點，全部喺你部 Mac 入面運算，錄音同文字都唔會上傳。

## 安裝

開 **Terminal**（Spotlight 搜「Terminal」），貼呢一行，撳 Enter：

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/tinhohui/cantonese-dictation/main/install.sh)"
```

需要：Apple Silicon Mac（M1 或以上）、約 10GB 硬碟空間、建議 16GB RAM。
第一次安裝要下載約 8GB 模型，視乎網速要 10–30 分鐘。

中途會問一次你部 Mac 嘅登入密碼（裝 Homebrew 用，打字時唔會顯示）。

裝完之後有兩個權限要你親手批，macOS 規定冇得自動：

1. **系統設定 → 私隱與保安 → 輔助使用** → 打開 **Hammerspoon**，然後撳選單列
   🎙️ → 「重載設定」
2. 彈出「Python 想使用麥克風」→ 撳 **允許**

（如果彈出「Hammerspoon 係由互聯網下載嘅 App」，撳「打開」。）

## 點用

| 動作 | 效果 |
| --- | --- |
| 撳住 右⌘ 講嘢，鬆手 | 出字 |
| 輕撳一下 右⌘ | 長段落模式，再撳一下完成 |
| 選單列 🎙️ 圖示 | 轉語言（auto／粵語／English）、開關潤色、睇返最近轉寫 |
| <http://127.0.0.1:8765> | 字典、歷史記錄、加其他觸發鍵（例如滑鼠側鍵） |

專有名詞識別錯（人名、公司名、術語）：開 <http://127.0.0.1:8765>，喺字典度加一條
「聽錯嘅寫法 → 正確寫法」，之後就會自動改正。

## 要知道嘅嘢

- 選單列嘅橙色咪點會長期亮住。系統一直開住咪等你撳掣，但只有撳住右⌘
  嗰陣先會錄音同識別。
- 所有資料留喺 `~/dictation`：`history.jsonl`（轉寫記錄）、`recordings/`
  （最近嘅錄音）、`dictionary.json`（你嘅字典）。
- 冇 internet 都用得。
- 「自動學字」（出字之後你即刻改正，系統自己記低）要部機有裝 Claude Code
  （`~/.local/bin/claude`）先會生效，會用少量你自己嘅 Claude 額度。冇裝嘅話
  改正只會排隊，唔會自動入字典 —— 語音輸入本身唔受影響，字典可以手動加。

## 出問題

- **撳右⌘冇反應** → 檢查「輔助使用」入面 Hammerspoon 有冇開；再撳選單列
  🎙️ → 「重載設定」。
- **有反應但冇字** → 系統設定 → 私隱與保安 → 麥克風，開啟 Python；
  再撳 🎙️ → 「🩹 修復語音輸入」。
- **仲係唔得** → 撳「🩹 修復語音輸入」之後，將 `~/dictation/last-fix-bundle.txt`
  send 畀分享呢個系統俾你嘅朋友。
- **更新** → 再貼一次上面嗰行安裝指令。你嘅字典、快捷鍵、記錄都唔會被覆蓋。

## 移除

```bash
launchctl bootout gui/$(id -u)/com.huitinho.dictation; rm -f ~/Library/LaunchAgents/com.huitinho.dictation.plist
```

之後退出 Hammerspoon，刪除 `~/dictation` 同 `~/.hammerspoon/init.lua` 就得。
