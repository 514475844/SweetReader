// SweetReader 主控制脚本
document.addEventListener('DOMContentLoaded', function() {
    console.log('SweetReader 已加载');
    
    // 加载书籍列表
    loadBooks();
    
    // 加载分类
    loadCategories();
    
    // 搜索功能
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
        searchInput.addEventListener('keyup', function(e) {
            if (e.key === 'Enter') {
                searchBooks(this.value);
            }
        });
    }
    
    // 格式筛选
    const formatFilter = document.getElementById('formatFilter');
    if (formatFilter) {
        formatFilter.addEventListener('change', function() {
            loadBooks(this.value);
        });
    }
    
    // 随机推荐
    const randomBtn = document.getElementById('randomBtn');
    if (randomBtn) {
        randomBtn.addEventListener('click', function() {
            window.location.href = '/random';
        });
    }
});

function loadBooks(format) {
    const url = format ? `/api/books?format=${format}` : '/api/books';
    fetch(url)
        .then(response => response.json())
        .then(data => {
            const container = document.getElementById('bookContainer');
            if (!container) return;
            if (data.books && data.books.length > 0) {
                container.innerHTML = data.books.map(book => `
                    <div class="book-card" onclick="location.href='/read/${book.id}'">
                        <h3>${book.title}</h3>
                        <p>${book.author || '未知作者'}</p>
                        <small>${book.format}</small>
                    </div>
                `).join('');
            } else {
                container.innerHTML = '<p>没有找到书籍</p>';
            }
        })
        .catch(error => console.error('加载书籍失败:', error));
}

function loadCategories() {
    fetch('/api/categories')
        .then(response => response.json())
        .then(data => {
            const container = document.getElementById('categoryList');
            if (!container) return;
            if (data.categories) {
                container.innerHTML = data.categories.map(cat => `
                    <a href="/category/${cat.id}">${cat.name}</a>
                `).join('');
            }
        })
        .catch(error => console.error('加载分类失败:', error));
}

function searchBooks(query) {
    if (!query.trim()) return loadBooks();
    fetch(`/api/search?q=${encodeURIComponent(query)}`)
        .then(response => response.json())
        .then(data => {
            const container = document.getElementById('bookContainer');
            if (!container) return;
            if (data.books && data.books.length > 0) {
                container.innerHTML = data.books.map(book => `
                    <div class="book-card" onclick="location.href='/read/${book.id}'">
                        <h3>${book.title}</h3>
                        <p>${book.author || '未知作者'}</p>
                    </div>
                `).join('');
            } else {
                container.innerHTML = '<p>未找到相关书籍</p>';
            }
        })
        .catch(error => console.error('搜索失败:', error));
}
