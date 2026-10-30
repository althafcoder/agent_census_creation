// ==========================================================================
// RAPT Census & Invoice Comparison UI Logic
// ==========================================================================

document.addEventListener("DOMContentLoaded", () => {
  // DOM Elements - Navigation & Modes
  const tabBtnUpload = document.getElementById("tab-btn-upload");
  const tabBtnFolder = document.getElementById("tab-btn-folder");
  const modeUpload = document.getElementById("mode-upload");
  const modeFolder = document.getElementById("mode-folder");
  const btnQuickSample = document.getElementById("btn-quick-sample");

  // DOM Elements - Upload & Files
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const filesPreview = document.getElementById("files-preview-section");
  const previewCount = document.getElementById("preview-count");
  const fileList = document.getElementById("file-list");
  const btnClearFiles = document.getElementById("btn-clear-files");
  const btnProcess = document.getElementById("btn-process");

  // DOM Elements - Folder Mode
  const folderPathInput = document.getElementById("folder-path-input");
  const btnProcessFolder = document.getElementById("btn-process-folder");
  const presetButtons = document.querySelectorAll(".btn-preset");

  // DOM Elements - States & Results
  const stateIdle = document.getElementById("state-idle");
  const stateLoading = document.getElementById("state-loading");
  const stateResults = document.getElementById("state-results");
  const progressBar = document.getElementById("progress-bar");
  const loaderStatus = document.getElementById("loader-status");
  const loaderSub = document.getElementById("loader-sub");

  // Result Elements
  const btnDownloadXlsx = document.getElementById("btn-download-xlsx");
  const btnDownloadTxt = document.getElementById("btn-download-txt");
  const outputCompanyName = document.getElementById("output-company-name");
  const outputMeta = document.getElementById("output-meta");
  const summaryText = document.getElementById("summary-text");
  const btnCopySummary = document.getElementById("btn-copy-summary");
  const classifiedDocsBody = document.getElementById("classified-docs-body");
  const docsCount = document.getElementById("docs-count");
  const notesList = document.getElementById("notes-list");
  const toast = document.getElementById("toast");
  const themeToggle = document.getElementById("theme-toggle");

  // --------------------------------------------------------------------------
  // Theme Switching (Light Blue & White vs Dark Mode)
  // --------------------------------------------------------------------------
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    document.body.setAttribute("data-theme", theme);
    localStorage.setItem("census_theme", theme);
  }

  // Load saved theme or default to dark
  const savedTheme = localStorage.getItem("census_theme") || "dark";
  applyTheme(savedTheme);

  if (themeToggle) {
    themeToggle.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") || "dark";
      const nextTheme = current === "light" ? "dark" : "light";
      applyTheme(nextTheme);
      showToast(nextTheme === "light" ? "☀️ Light mode activated (Light Blue & White)" : "🌙 Dark mode activated");
    });
  }


  // State
  let stagedFiles = [];
  let progressInterval = null;

  // --------------------------------------------------------------------------
  // Tab Switching
  // --------------------------------------------------------------------------
  tabBtnUpload.addEventListener("click", () => {
    tabBtnUpload.classList.add("active");
    tabBtnFolder.classList.remove("active");
    modeUpload.style.display = "block";
    modeFolder.style.display = "none";
  });

  tabBtnFolder.addEventListener("click", () => {
    tabBtnFolder.classList.add("active");
    tabBtnUpload.classList.remove("active");
    modeFolder.style.display = "block";
    modeUpload.style.display = "none";
  });

  // Preset buttons
  presetButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      folderPathInput.value = btn.dataset.path;
      folderPathInput.focus();
    });
  });

  // --------------------------------------------------------------------------
  // File Staging & Drag/Drop
  // --------------------------------------------------------------------------
  ["dragenter", "dragover"].forEach(event => {
    dropzone.addEventListener(event, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach(event => {
    dropzone.addEventListener(event, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("dragover");
    });
  });

  dropzone.addEventListener("drop", (e) => {
    const dropped = e.dataTransfer?.files;
    if (dropped && dropped.length) {
      addFiles(dropped);
    }
  });

  fileInput.addEventListener("change", (e) => {
    if (e.target.files && e.target.files.length) {
      addFiles(e.target.files);
    }
  });

  function guessRole(filename) {
    const fn = filename.toLowerCase();
    if (/ghq|questionnaire|rfp/.test(fn)) return { role: "ghq", label: "GHQ / RFP" };
    if (/invoice|bill|statement|premium/.test(fn)) return { role: "invoice", label: "Invoice" };
    if (/renewal|renew|rate|proposal/.test(fn)) return { role: "renewal", label: "Renewal" };
    if (/census|roster|enroll|dependent|employee/.test(fn)) return { role: "census", label: "Census" };
    return { role: "census", label: "Census / Doc" };
  }

  function formatBytes(bytes) {
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
  }

  function addFiles(newFiles) {
    for (let f of newFiles) {
      // Prevent duplicates by filename and size
      if (!stagedFiles.some(existing => existing.name === f.name && existing.size === f.size)) {
        stagedFiles.push(f);
      }
    }
    renderStagedFiles();
  }

  function renderStagedFiles() {
    fileList.innerHTML = "";
    if (stagedFiles.length === 0) {
      filesPreview.style.display = "none";
      btnProcess.disabled = true;
      return;
    }

    filesPreview.style.display = "block";
    btnProcess.disabled = false;
    previewCount.textContent = `${stagedFiles.length} file${stagedFiles.length > 1 ? "s" : ""} selected`;

    stagedFiles.forEach((file, index) => {
      const li = document.createElement("li");
      li.className = "file-item";

      const roleInfo = guessRole(file.name);

      li.innerHTML = `
        <div class="file-item-left">
          <svg class="file-item-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
            <polyline points="14 2 14 8 20 8"></polyline>
          </svg>
          <div>
            <div class="file-item-name" title="${file.name}">${file.name}</div>
            <div class="file-item-size">${formatBytes(file.size)}</div>
          </div>
        </div>
        <div style="display: flex; align-items: center; gap: 8px;">
          <span class="file-item-role role-${roleInfo.role}">${roleInfo.label}</span>
          <button class="btn-remove-file" data-index="${index}" title="Remove file">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </div>
      `;

      li.querySelector(".btn-remove-file").addEventListener("click", (e) => {
        e.stopPropagation();
        stagedFiles.splice(index, 1);
        renderStagedFiles();
      });

      fileList.appendChild(li);
    });
  }

  btnClearFiles.addEventListener("click", () => {
    stagedFiles = [];
    fileInput.value = "";
    renderStagedFiles();
  });

  // --------------------------------------------------------------------------
  // Pipeline Execution
  // --------------------------------------------------------------------------

  // Mode 1: Upload and run
  btnProcess.addEventListener("click", async () => {
    if (!stagedFiles.length) return;

    const formData = new FormData();
    stagedFiles.forEach(f => formData.append("files", f));

    startPipelineProgress();

    try {
      const res = await fetch("/api/process", {
        method: "POST",
        body: formData,
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || "Pipeline execution failed.");
      }

      const data = await res.json();
      displayResults(data);
    } catch (err) {
      handlePipelineError(err.message);
    }
  });

  // Mode 2: Test Folder
  presetButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      const p = btn.getAttribute("data-path");
      if (p) folderPathInput.value = p;
    });
  });

  btnProcessFolder.addEventListener("click", async () => {
    const path = folderPathInput.value.trim();
    if (!path) {
      showToast("Please enter a valid directory path.");
      return;
    }

    startPipelineProgress();

    try {
      const res = await fetch("/api/test-folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder_path: path }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || "Folder test failed.");
      }

      const data = await res.json();
      displayResults(data);
    } catch (err) {
      handlePipelineError(err.message);
    }
  });

  // Header: Quick Sample Test
  btnQuickSample.addEventListener("click", async () => {
    startPipelineProgress();
    try {
      const res = await fetch("/api/test-sample", { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || "Sample test failed.");
      }
      const data = await res.json();
      displayResults(data);
    } catch (err) {
      handlePipelineError(err.message);
    }
  });

  // --------------------------------------------------------------------------
  // Loading & Progress Animations
  // --------------------------------------------------------------------------
  function startPipelineProgress() {
    stateIdle.style.display = "none";
    stateResults.style.display = "none";
    stateLoading.style.display = "block";

    progressBar.style.width = "10%";
    const steps = [
      { id: "step-1", pct: "25%", text: "Reading & Classifying Documents" },
      { id: "step-2", pct: "45%", text: "Consolidating Census & Matching Dependents" },
      { id: "step-3", pct: "65%", text: "Extracting Invoice Subscribers" },
      { id: "step-4", pct: "85%", text: "Applying Underwriting Rules & Rate Tables" },
      { id: "step-5", pct: "95%", text: "Generating Master Excel Workbook & Exceptions" },
    ];

    let currentStep = 0;
    // Reset steps
    document.querySelectorAll(".step-item").forEach(s => {
      s.className = "step-item";
    });
    document.getElementById("step-1").classList.add("active");

    if (progressInterval) clearInterval(progressInterval);
    progressInterval = setInterval(() => {
      if (currentStep < steps.length) {
        const s = steps[currentStep];
        progressBar.style.width = s.pct;
        loaderStatus.textContent = s.text;

        document.querySelectorAll(".step-item").forEach((el, idx) => {
          if (idx < currentStep) el.className = "step-item done";
          else if (idx === currentStep) el.className = "step-item active";
          else el.className = "step-item";
        });
        currentStep++;
      }
    }, 1200);
  }

  function handlePipelineError(errorMsg) {
    if (progressInterval) clearInterval(progressInterval);
    stateLoading.style.display = "none";
    stateIdle.style.display = "block";
    showToast(`Error: ${errorMsg}`);
  }

  // --------------------------------------------------------------------------
  // Results Display
  // --------------------------------------------------------------------------
  function displayResults(data) {
    if (progressInterval) clearInterval(progressInterval);
    progressBar.style.width = "100%";

    setTimeout(() => {
      stateLoading.style.display = "none";
      stateResults.style.display = "block";

      // Download links
      btnDownloadXlsx.href = data.download_xlsx_url;
      btnDownloadTxt.href = data.download_txt_url;

      // Dynamic label based on carrier
      const btnXlsxLabel = document.getElementById("btn-download-xlsx-label");
      if (btnXlsxLabel) {
        btnXlsxLabel.textContent = data.carrier === "Cigna" ? "Download Marsh Census (.xlsx)" : "Download RAPT Census (.xlsx)";
      }

      // SBU download button (Cigna deliverable)
      const btnDownloadSbu = document.getElementById("btn-download-sbu");
      if (btnDownloadSbu) {
        if (data.download_sbu_url) {
          btnDownloadSbu.style.display = "inline-flex";
          btnDownloadSbu.href = data.download_sbu_url;
        } else {
          btnDownloadSbu.style.display = "none";
        }
      }

      // Email download button & text (Intake review deliverable)
      const btnDownloadEmail = document.getElementById("btn-download-email");
      if (btnDownloadEmail) {
        if (data.download_email_url) {
          btnDownloadEmail.style.display = "inline-flex";
          btnDownloadEmail.href = data.download_email_url;
        } else {
          btnDownloadEmail.style.display = "none";
        }
      }
      const emailTextEl = document.getElementById("email-text");
      if (emailTextEl) {
        emailTextEl.textContent = data.email_text || "No missing information email generated.";
      }

      // Carrier badge
      const carrierBadge = document.getElementById("carrier-badge");
      if (carrierBadge && data.carrier) {
        carrierBadge.style.display = "inline-flex";
        carrierBadge.textContent = `Carrier: ${data.carrier}${data.state ? ' (' + data.state + ')' : ''}`;
      }

      outputCompanyName.textContent = data.output_filename;
      outputMeta.textContent = `Completed Census with ${data.audit?.out_employees || 0} employees and ${data.audit?.out_dependents || 0} dependents.`;

      // Metrics
      const audit = data.audit || {};
      const counters = data.counters || {};

      document.getElementById("m-employees").textContent = audit.out_employees ?? 0;
      document.getElementById("m-src-employees").textContent = `Source rows: ${audit.src_employees ?? 0}`;
      document.getElementById("m-dependents").textContent = audit.out_dependents ?? 0;
      document.getElementById("m-unassigned").textContent = `Source rows: ${audit.src_dependents ?? 0}`;

      document.getElementById("m-missing-invoice").textContent = counters.missing_invoice ?? 0;
      document.getElementById("m-missing-census").textContent = counters.missing_census ?? 0;
      document.getElementById("m-tier-mismatches").textContent = counters.tier_mismatch ?? 0;
      document.getElementById("m-duplicates").textContent = audit.duplicate_employees ?? counters.duplicate_employees ?? 0;

      // Summary text
      summaryText.textContent = data.summary || "No exceptions recorded.";

      // Classified docs table
      classifiedDocsBody.innerHTML = "";
      const docs = data.classified_docs || [];
      docsCount.textContent = docs.length;

      if (docs.length === 0) {
        classifiedDocsBody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No documents processed</td></tr>`;
      } else {
        docs.forEach(d => {
          const tr = document.createElement("tr");
          const isOcr = d.scanned || (d.extraction_method && d.extraction_method.includes("PaddleOCR"));
          const modeTag = isOcr 
            ? `<span style="color: var(--amber); font-weight: 600;">⚡ Scanned Document</span>` 
            : `<span style="color: var(--emerald); font-weight: 500;">📄 ${d.extraction_method || 'Digital Extraction'}</span>`;
          
          const charStr = (d.char_count || 0).toLocaleString();
          tr.innerHTML = `
            <td style="font-family: var(--font-mono); font-weight: 500;">${d.name}</td>
            <td><span class="file-item-role role-${d.role}">${d.role}</span></td>
            <td>${modeTag}</td>
            <td style="font-family: var(--font-mono); font-size: 0.8rem; color: var(--text-muted);">${charStr} chars</td>
            <td>
              <button class="btn-view-text" data-name="${encodeURIComponent(d.name)}" title="Inspect extracted text">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
                  <circle cx="12" cy="12" r="3"></circle>
                </svg>
                <span>View Text</span>
              </button>
            </td>
          `;

          const viewBtn = tr.querySelector(".btn-view-text");
          viewBtn.addEventListener("click", () => {
            openTextModal(d);
          });

          classifiedDocsBody.appendChild(tr);
        });
      }

      // Notes list
      notesList.innerHTML = "";
      const notes = data.notes || [];
      if (notes.length === 0) {
        notesList.innerHTML = `<li class="note-item" style="border-left-color: var(--emerald);">No discrepancies or special document flags encountered.</li>`;
      } else {
        notes.forEach(n => {
          const li = document.createElement("li");
          li.className = "note-item";
          li.innerHTML = `<span>⚠️</span><span>${n}</span>`;
          notesList.appendChild(li);
        });
      }

      showToast("Pipeline completed successfully!");
    }, 400);
  }

  // --------------------------------------------------------------------------
  // Results Tabs
  // --------------------------------------------------------------------------
  const resultTabs = document.querySelectorAll(".result-tab");
  resultTabs.forEach(tab => {
    tab.addEventListener("click", () => {
      resultTabs.forEach(t => t.classList.remove("active"));
      tab.classList.add("active");

      document.querySelectorAll(".result-tab-pane").forEach(pane => {
        pane.style.display = "none";
        pane.classList.remove("active");
      });

      const targetId = tab.dataset.target;
      const targetPane = document.getElementById(targetId);
      if (targetPane) {
        targetPane.style.display = "block";
        targetPane.classList.add("active");
      }
    });
  });

  // Copy Summary to Clipboard
  btnCopySummary.addEventListener("click", () => {
    const text = summaryText.textContent;
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => {
      showToast("Exception Summary copied to clipboard!");
    });
  });

  // Copy Missing Information Email to Clipboard
  const btnCopyEmail = document.getElementById("btn-copy-email");
  if (btnCopyEmail) {
    btnCopyEmail.addEventListener("click", () => {
      const emailEl = document.getElementById("email-text");
      const text = emailEl ? emailEl.textContent : "";
      if (!text) return;
      navigator.clipboard.writeText(text).then(() => {
        showToast("Missing Information Email copied to clipboard!");
      });
    });
  }

  // --------------------------------------------------------------------------
  // Extracted Text Modal Viewer
  // --------------------------------------------------------------------------
  const textModal = document.getElementById("text-modal");
  const modalCloseBtn = document.getElementById("modal-close-btn");
  const modalDocTitle = document.getElementById("modal-doc-title");
  const modalDocSub = document.getElementById("modal-doc-sub");
  const modalStats = document.getElementById("modal-stats");
  const modalPreContent = document.getElementById("modal-pre-content");
  const btnCopyModalText = document.getElementById("btn-copy-modal-text");
  const btnDownloadModalFile = document.getElementById("btn-download-modal-file");

  function openTextModal(doc) {
    if (!textModal) return;
    modalDocTitle.textContent = doc.name;
    const isOcr = doc.scanned || (doc.extraction_method && doc.extraction_method.includes("PaddleOCR"));
    modalDocSub.textContent = isOcr ? "Extracted via OCR (Scanned Document)" : `Extracted via ${doc.extraction_method || 'Digital Parser'}`;
    modalStats.textContent = `${(doc.char_count || 0).toLocaleString()} characters extracted${doc.table_count ? ` | ${doc.table_count} tables` : ''}`;
    modalPreContent.textContent = "Loading extracted text from disk...";
    
    const downloadFilename = doc.extracted_filename || (doc.name.replace(/\.[^/.]+$/, "").replace(/ /g, "_") + "_extracted.txt");
    btnDownloadModalFile.href = `/api/download-extracted/${encodeURIComponent(downloadFilename)}`;
    textModal.style.display = "flex";

    // Fetch full extracted text content from API
    fetch(`/api/extracted-text/${encodeURIComponent(doc.name)}`)
      .then(res => {
        if (!res.ok) throw new Error("Could not load text");
        return res.json();
      })
      .then(data => {
        modalPreContent.textContent = data.content;
      })
      .catch(() => {
        modalPreContent.textContent = doc.text_snippet || "[Preview text unavailable]";
      });
  }

  if (modalCloseBtn) {
    modalCloseBtn.addEventListener("click", () => {
      textModal.style.display = "none";
    });
  }

  if (textModal) {
    textModal.addEventListener("click", (e) => {
      if (e.target === textModal) {
        textModal.style.display = "none";
      }
    });
  }

  if (btnCopyModalText) {
    btnCopyModalText.addEventListener("click", () => {
      const text = modalPreContent.textContent;
      if (!text) return;
      navigator.clipboard.writeText(text).then(() => {
        showToast("Extracted text copied to clipboard!");
      });
    });
  }

  // --------------------------------------------------------------------------
  // Toast Helper
  // --------------------------------------------------------------------------
  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.add("show");
    setTimeout(() => {
      toast.classList.remove("show");
    }, 3500);
  }

});
