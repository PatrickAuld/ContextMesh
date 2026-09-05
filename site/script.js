"use strict";

const tabList = document.querySelector(".scenario-tabs");
const tabs = [...tabList.querySelectorAll("button")];
const panels = tabs.map((tab) => document.getElementById(tab.getAttribute("aria-controls")));

function selectTab(index, focus = false) {
  tabs.forEach((tab, i) => {
    const selected = i === index;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    tab.classList.toggle("selected", selected);
    panels[i].hidden = !selected;
  });
  if (focus) tabs[index].focus();
}

tabList.setAttribute("role", "tablist");
tabs.forEach((tab, index) => {
  tab.setAttribute("role", "tab");
  panels[index].setAttribute("role", "tabpanel");
  panels[index].tabIndex = 0;
  tab.addEventListener("click", () => selectTab(index));
  tab.addEventListener("keydown", (event) => {
    let next;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabs.length - 1;
    if (next !== undefined) {
      event.preventDefault();
      selectTab(next, true);
    }
  });
});
selectTab(0);

const copyButton = document.querySelector(".copy-button");
if (navigator.clipboard && window.isSecureContext) {
  copyButton.hidden = false;
  copyButton.addEventListener("click", async () => {
    const status = document.querySelector(".copy-status");
    try {
      await navigator.clipboard.writeText(document.getElementById("sdk-code").textContent);
      status.textContent = "Python example copied.";
      copyButton.textContent = "Copied";
      setTimeout(() => { copyButton.textContent = "Copy"; status.textContent = ""; }, 2500);
    } catch {
      status.textContent = "Copy unavailable. Select and copy the example directly.";
    }
  });
}
