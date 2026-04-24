import { jsPDF } from "jspdf";

function normalizeMarkdown(markdown) {
  return (markdown || "")
    .replace(/\r\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function stripInlineMarkdown(text) {
  return text
    .replace(/\*\*(.*?)\*\*/g, "$1")
    .replace(/\*(.*?)\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
}

async function tryRegisterDejaVu(doc) {
  try {
    const response = await fetch("/fonts/DejaVu%20Sans.ttf");
    if (!response.ok) return false;
    const buffer = await response.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    if (bytes.length < 4) return false;

    const isTrueType =
      (bytes[0] === 0x00 && bytes[1] === 0x01 && bytes[2] === 0x00 && bytes[3] === 0x00) ||
      (bytes[0] === 0x4f && bytes[1] === 0x54 && bytes[2] === 0x54 && bytes[3] === 0x4f);
    if (!isTrueType) {
      return false;
    }

    let binary = "";
    for (let i = 0; i < bytes.length; i += 1) {
      binary += String.fromCharCode(bytes[i]);
    }
    const base64 = btoa(binary);
    doc.addFileToVFS("DejaVu Sans.ttf", base64);
    doc.addFont("DejaVu Sans.ttf", "DejaVu", "normal");
    doc.setFont("DejaVu", "normal");
    return true;
  } catch (_error) {
    return false;
  }
}

export async function generatePdf(markdown) {
  const doc = new jsPDF({ unit: "mm", format: "a4", orientation: "portrait" });
  const useDejaVu = await tryRegisterDejaVu(doc);
  if (!useDejaVu) {
    doc.setFont("times", "normal");
  }

  const pageWidth = doc.internal.pageSize.getWidth();
  const pageHeight = doc.internal.pageSize.getHeight();
  const marginLeft = 20;
  const marginTop = 20;
  const marginBottom = 20;
  const maxTextWidth = pageWidth - marginLeft * 2;
  const lineHeight = 6;
  let y = marginTop;

  const content = normalizeMarkdown(markdown);
  const lines = content.split("\n");

  const writeWrapped = (text, size = 12, gapAfter = 2) => {
    const prepared = stripInlineMarkdown(text || " ");
    doc.setFontSize(size);
    const wrapped = doc.splitTextToSize(prepared, maxTextWidth);
    wrapped.forEach((line) => {
      if (y + lineHeight > pageHeight - marginBottom) {
        doc.addPage();
        y = marginTop;
      }
      doc.text(line, marginLeft, y);
      y += lineHeight;
    });
    y += gapAfter;
  };

  lines.forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) {
      y += 2;
      if (y > pageHeight - marginBottom) {
        doc.addPage();
        y = marginTop;
      }
      return;
    }

    if (trimmed.startsWith("## ")) {
      writeWrapped(trimmed.replace(/^##\s+/, ""), 14, 3);
      return;
    }

    if (trimmed.startsWith("# ")) {
      writeWrapped(trimmed.replace(/^#\s+/, ""), 16, 3);
      return;
    }

    if (/^[-*+]\s+/.test(trimmed)) {
      writeWrapped(`- ${trimmed.replace(/^[-*+]\s+/, "")}`, 12, 1);
      return;
    }

    writeWrapped(trimmed, 12, 1);
  });

  doc.save("analysis.pdf");
}
