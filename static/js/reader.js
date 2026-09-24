// 阅读器控制脚本
document.addEventListener('DOMContentLoaded', function() {
    console.log('阅读器已加载');
    const content = document.getElementById('txtContent');
    if (!content) return;
    
    // 从 localStorage 读取设置
    function loadSettings() {
        return {
            fontSize: parseInt(localStorage.getItem('reader_fontSize')) || 17,
            lineSpacing: parseFloat(localStorage.getItem('reader_lineSpacing')) || 2.0,
            theme: localStorage.getItem('reader_theme') || 'light'
        };
    }
    
    function applySettings(settings) {
        content.style.fontSize = settings.fontSize + 'px';
        content.style.lineHeight = settings.lineSpacing;
        // 主题
        if (settings.theme === 'dark') {
            document.body.style.background = '#1a1a2e';
            document.body.style.color = '#e0e0e0';
            content.style.background = '#1a1a2e';
            content.style.color = '#e0e0e0';
        } else if (settings.theme === 'eye') {
            document.body.style.background = '#c7edcc';
            document.body.style.color = '#1a1a2e';
            content.style.background = '#c7edcc';
            content.style.color = '#1a1a2e';
        } else {
            document.body.style.background = '#f5f5f5';
            document.body.style.color = '#1a1a2e';
            content.style.background = '#ffffff';
            content.style.color = '#1a1a2e';
        }
    }
    
    // 初始化
    applySettings(loadSettings());
    
    // 时钟
    function updateClock() {
        const el = document.getElementById('readerTime');
        if (el) {
            const now = new Date();
            el.textContent = now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
        }
    }
    updateClock();
    setInterval(updateClock, 10000);
});

// ===== 全局函数（供 HTML onclick 调用） =====

function updateFontSize(val) {
    const content = document.getElementById('txtContent');
    const display = document.getElementById('fontSizeValue');
    if (content) content.style.fontSize = val + 'px';
    if (display) display.textContent = val;
}

function updateLineSpacing(val) {
    const content = document.getElementById('txtContent');
    const display = document.getElementById('lineSpacingValue');
    if (content) content.style.lineHeight = val;
    if (display) display.textContent = parseFloat(val).toFixed(1);
}

function saveAndClose() {
    const range = document.getElementById('fontSizeRange');
    const range2 = document.getElementById('lineSpacingRange');
    const content = document.getElementById('txtContent');
    
    if (range) {
        const val = range.value;
        localStorage.setItem('reader_fontSize', val);
        if (content) content.style.fontSize = val + 'px';
    }
    if (range2) {
        const val = range2.value;
        localStorage.setItem('reader_lineSpacing', val);
        if (content) content.style.lineHeight = val;
    }
    
    const panel = document.getElementById('settingsPanel');
    if (panel) panel.style.display = 'none';
    
    // Toast 提示
    showToast('✅ 已保存');
}

function resetAndClose() {
    const content = document.getElementById('txtContent');
    const range = document.getElementById('fontSizeRange');
    const range2 = document.getElementById('lineSpacingRange');
    const display = document.getElementById('fontSizeValue');
    const display2 = document.getElementById('lineSpacingValue');
    
    const defaultFontSize = 17;
    const defaultLineSpacing = 2.0;
    
    if (content) {
        content.style.fontSize = defaultFontSize + 'px';
        content.style.lineHeight = defaultLineSpacing;
    }
    if (range) range.value = defaultFontSize;
    if (range2) range2.value = defaultLineSpacing;
    if (display) display.textContent = defaultFontSize;
    if (display2) display2.textContent = defaultLineSpacing.toFixed(1);
    
    localStorage.setItem('reader_fontSize', defaultFontSize);
    localStorage.setItem('reader_lineSpacing', defaultLineSpacing);
    
    const panel = document.getElementById('settingsPanel');
    if (panel) panel.style.display = 'none';
    
    showToast('↩️ 已重置');
}

function toggleSettings() {
    const panel = document.getElementById('settingsPanel');
    if (!panel) return;
    
    if (panel.style.display === 'none' || panel.style.display === '') {
        // 打开时加载已保存的值
        const savedFontSize = localStorage.getItem('reader_fontSize') || 17;
        const savedLineSpacing = localStorage.getItem('reader_lineSpacing') || 2.0;
        const range = document.getElementById('fontSizeRange');
        const range2 = document.getElementById('lineSpacingRange');
        const display = document.getElementById('fontSizeValue');
        const display2 = document.getElementById('lineSpacingValue');
        const content = document.getElementById('txtContent');
        
        if (range) range.value = savedFontSize;
        if (range2) range2.value = savedLineSpacing;
        if (display) display.textContent = savedFontSize;
        if (display2) display2.textContent = parseFloat(savedLineSpacing).toFixed(1);
        if (content) {
            content.style.fontSize = savedFontSize + 'px';
            content.style.lineHeight = savedLineSpacing;
        }
        panel.style.display = 'block';
    } else {
        // 关闭时恢复到已保存的值（放弃修改）
        const savedFontSize = localStorage.getItem('reader_fontSize') || 17;
        const savedLineSpacing = localStorage.getItem('reader_lineSpacing') || 2.0;
        const content = document.getElementById('txtContent');
        if (content) {
            content.style.fontSize = savedFontSize + 'px';
            content.style.lineHeight = savedLineSpacing;
        }
        panel.style.display = 'none';
    }
}

function toggleTheme() {
    const themes = ['light', 'dark', 'eye'];
    const current = localStorage.getItem('reader_theme') || 'light';
    let idx = themes.indexOf(current);
    idx = (idx + 1) % themes.length;
    const newTheme = themes[idx];
    localStorage.setItem('reader_theme', newTheme);
    
    const content = document.getElementById('txtContent');
    if (!content) return;
    
    if (newTheme === 'dark') {
        document.body.style.background = '#1a1a2e';
        document.body.style.color = '#e0e0e0';
        content.style.background = '#1a1a2e';
        content.style.color = '#e0e0e0';
    } else if (newTheme === 'eye') {
        document.body.style.background = '#c7edcc';
        document.body.style.color = '#1a1a2e';
        content.style.background = '#c7edcc';
        content.style.color = '#1a1a2e';
    } else {
        document.body.style.background = '#f5f5f5';
        document.body.style.color = '#1a1a2e';
        content.style.background = '#ffffff';
        content.style.color = '#1a1a2e';
    }
    showToast('🌓 ' + (newTheme === 'dark' ? '深色' : newTheme === 'eye' ? '护眼' : '明亮'));
}

function showToast(msg) {
    let toast = document.getElementById('toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'toast';
        toast.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,0.8);color:#fff;padding:8px 20px;border-radius:6px;font-size:13px;z-index:9999;transition:opacity 0.3s;opacity:0;pointer-events:none;';
        document.body.appendChild(toast);
    }
    toast.textContent = msg;
    toast.style.opacity = '1';
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function() { toast.style.opacity = '0'; }, 1200);
}
