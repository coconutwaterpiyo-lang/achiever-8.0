async function loadFolders() {
    const response = await fetch("data.json");
    const data = await response.json();

    const container = document.getElementById("folders");
    container.innerHTML = "";

    data.folders.forEach(folder => {
        const div = document.createElement("div");
        div.className = "folder";
        div.innerHTML = `
            <div class="folder-name">📁 ${folder.name}</div>
            <div class="arrow">➜</div>
        `;
        container.appendChild(div);
    });
}

loadFolders();
