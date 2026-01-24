/**
 * Shared AI Image & Datasheet Search functionality
 * Works across index.html (add form) and part_detail.html (edit)
 */

// State for current search operation
let imageSearchState = {
    targetInputId: null,      // ID of the input field to fill
    partUuid: null,           // Part UUID (for detail page edits)
    partDescription: null,    // Query for search
    type: 'part',             // 'part', 'pinout', or 'datasheet'
    onComplete: null          // Callback after selection
};

/**
 * Check if AI is enabled (looks for data attribute or global variable)
 */
function isAiEnabled() {
    // Check for autofill button's data attribute (index page)
    const btn = document.getElementById("btn-autofill");
    if (btn && btn.dataset.aiEnabled === "true") return true;

    // Check for global variable (detail page)
    if (typeof window.AI_ENABLED !== 'undefined') return window.AI_ENABLED;

    return false;
}

/**
 * Open AI image search modal
 * @param {string} type - 'part' or 'pinout'
 * @param {object} options - { targetInputId, partUuid, partDescription, onComplete }
 */
async function openImageSearchModal(type, options = {}) {
    if (!isAiEnabled()) {
        alert("AI features are not available. Please configure OPENAI_API_KEY and TAVILY_API_KEY.");
        return;
    }

    const description = options.partDescription || document.getElementById("part-description")?.value?.trim();
    if (!description || description.length < 3) {
        alert("Part description is required for AI search (at least 3 characters).");
        return;
    }

    // Store state
    imageSearchState = {
        targetInputId: options.targetInputId || (type === 'pinout' ? 'part-pinout-url' : 'part-image-url'),
        partUuid: options.partUuid || null,
        partDescription: description,
        type: type,
        onComplete: options.onComplete || null
    };

    const grid = document.getElementById("imageGrid");
    const spinner = document.getElementById("imageLoading");
    const title = document.getElementById("imagePickerTitle");
    if (!grid || !spinner) return;

    // Update title
    if (title) {
        title.textContent = type === 'pinout' ? 'Select Pinout Image' : 'Select Device Image';
    }

    // Reset and show loading
    grid.innerHTML = "";
    spinner.classList.remove("d-none");

    // Show modal
    const modalEl = document.getElementById("imagePickerModal");
    if (modalEl && window.bootstrap) {
        const modal = new bootstrap.Modal(modalEl);
        modal.show();
    }

    try {
        const res = await fetch(`/api/search-images?query=${encodeURIComponent(description)}&type=${type}`);
        const images = await res.json();

        spinner.classList.add("d-none");
        if (!images.length) {
            grid.innerHTML = '<p class="text-danger">No images found. Try a different description.</p>';
            return;
        }

        images.forEach(img => {
            const col = document.createElement("div");
            col.className = "col-6 col-md-4 col-lg-2";
            col.innerHTML = `
                <div class="card img-card h-100" data-url="${img.url}" onclick="selectAndDownloadImage(this)">
                    <img src="${img.url}" class="card-img-top preview-img" onerror="this.parentElement.style.display='none'">
                    <div class="card-body p-1 text-center">
                        <small class="text-muted" style="font-size:10px">${img.source || "Web"}</small>
                    </div>
                </div>`;
            grid.appendChild(col);
        });
    } catch (e) {
        spinner.classList.add("d-none");
        grid.innerHTML = '<p class="text-danger">Search failed. Please try again.</p>';
    }
}

/**
 * Handle image selection and download
 * @param {HTMLElement} cardEl - The clicked card element
 */
async function selectAndDownloadImage(cardEl) {
    const url = cardEl.dataset.url;
    const type = imageSearchState.type;
    const partDescription = imageSearchState.partDescription;

    // Show loading state
    const originalContent = cardEl.innerHTML;
    cardEl.innerHTML = '<div class="d-flex justify-content-center align-items-center h-100"><div class="spinner-border spinner-border-sm text-primary"></div></div>';
    cardEl.style.pointerEvents = "none";

    try {
        const res = await fetch("/api/download-image", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, type, part_description: partDescription })
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Download failed");
        }

        const data = await res.json();
        const filename = data.filename;

        // Set the filename in the target input
        const target = document.getElementById(imageSearchState.targetInputId);
        if (target) {
            target.value = filename;
        }

        // Close modal
        const modalEl = document.getElementById("imagePickerModal");
        if (modalEl && window.bootstrap) {
            const modal = bootstrap.Modal.getInstance(modalEl);
            if (modal) modal.hide();
        }

        // Call completion callback if provided (e.g., to save via HTMX)
        if (imageSearchState.onComplete) {
            imageSearchState.onComplete(filename);
        }

    } catch (e) {
        alert("Failed to download image: " + e.message);
        cardEl.innerHTML = originalContent;
        cardEl.style.pointerEvents = "";
    }
}

/**
 * Open AI datasheet search modal
 * @param {object} options - { targetInputId, partUuid, partDescription, onComplete }
 */
async function openDatasheetSearchModal(options = {}) {
    if (!isAiEnabled()) {
        alert("AI features are not available. Please configure OPENAI_API_KEY and TAVILY_API_KEY.");
        return;
    }

    const description = options.partDescription || document.getElementById("part-description")?.value?.trim();
    if (!description || description.length < 3) {
        alert("Part description is required for AI search (at least 3 characters).");
        return;
    }

    // Store state
    imageSearchState = {
        targetInputId: options.targetInputId || 'part-datasheet-url',
        partUuid: options.partUuid || null,
        partDescription: description,
        type: 'datasheet',
        onComplete: options.onComplete || null
    };

    const list = document.getElementById("datasheetList");
    const spinner = document.getElementById("datasheetLoading");
    if (!list || !spinner) return;

    // Reset and show loading
    list.innerHTML = "";
    spinner.classList.remove("d-none");

    // Show modal
    const modalEl = document.getElementById("datasheetPickerModal");
    if (modalEl && window.bootstrap) {
        const modal = new bootstrap.Modal(modalEl);
        modal.show();
    }

    try {
        const res = await fetch(`/api/search-datasheet?query=${encodeURIComponent(description)}`);
        const results = await res.json();

        spinner.classList.add("d-none");
        if (!results.length) {
            list.innerHTML = '<p class="text-muted p-3">No datasheets found. Try a different description.</p>';
            return;
        }

        results.forEach(item => {
            const div = document.createElement("a");
            div.href = "#";
            div.className = "list-group-item list-group-item-action d-flex justify-content-between align-items-start";
            div.onclick = (e) => {
                e.preventDefault();
                selectDatasheet(item.url);
            };
            div.innerHTML = `
                <div class="me-auto">
                    <div class="fw-bold">${escapeHtml(item.title)}</div>
                    <small class="text-muted">${escapeHtml(item.source)}</small>
                </div>
                <span class="badge bg-primary rounded-pill">Select</span>
            `;
            list.appendChild(div);
        });
    } catch (e) {
        spinner.classList.add("d-none");
        list.innerHTML = '<p class="text-danger p-3">Search failed. Please try again.</p>';
    }
}

/**
 * Handle datasheet selection
 * @param {string} url - The selected datasheet URL
 */
function selectDatasheet(url) {
    // Set URL in target input
    const target = document.getElementById(imageSearchState.targetInputId);
    if (target) {
        target.value = url;
    }

    // Close modal
    const modalEl = document.getElementById("datasheetPickerModal");
    if (modalEl && window.bootstrap) {
        const modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();
    }

    // Call completion callback
    if (imageSearchState.onComplete) {
        imageSearchState.onComplete(url);
    }
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Legacy function aliases for backward compatibility with index.html
// These will be called by the existing add form
function openImageSearch(type) {
    openImageSearchModal(type, {
        targetInputId: type === 'pinout' ? 'part-pinout-url' : 'part-image-url'
    });
}
