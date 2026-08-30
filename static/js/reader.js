// SweetReader 阅读器控制脚本
document.addEventListener('DOMContentLoaded', function() {
    console.log('阅读器已加载');
    
    // 字号控制
    const fontSizeBtn = document.getElementById('fontSizeBtn');
    if (fontSizeBtn) {
        let size = 17;
        fontSizeBtn.addEventListener('click', function() {
            const content = document.getElementById('bookContent');
            if (!content) return;
            size = size === 17 ? 20 : size === 20 ? 14 : 17;
            content.style.fontSize = size + 'px';
            savePreference('fontSize', size);
        });
    }
    
    // 主题切换
    const themeBtn = document.getElementById('themeBtn');
    if (themeBtn) {
        let themes = ['default', 'dark', 'eye'];
        let current = 0;
        themeBtn.addEventListener('click', function() {
            current = (current + 1) % themes.length;
            document.body.className = 'theme-' + themes[current];
            savePreference('theme', themes[current]);
        });
    }
    
    // 返回按钮
    const backBtn = document.getElementById('backBtn');
    if (backBtn) {
        backBtn.addEventListener('click', function() {
            window.location.href = '/';
        });
    }
    
    // 自动保存进度
    let saveTimer = null;
    const content = document.getElementById('bookContent');
    if (content) {
        content.addEventListener('scroll', function() {
            clearTimeout(saveTimer);
            saveTimer = setTimeout(function() {
                const progress = content.scrollTop / (content.scrollHeight - content.clientHeight);
                saveProgress(progress);
            }, 3000);
        });
    }
});

function savePreference(key, value) {
    fetch('/api/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key, value })
    }).catch(error => console.error('保存偏好失败:', error));
}

function saveProgress(progress) {
    const bookId = document.getElementById('bookId')?.value;
    if (!bookId) return;
    fetch('/api/progress', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ book_id: bookId, progress: progress })
    }).catch(error => console.error('保存进度失败:', error));
}
