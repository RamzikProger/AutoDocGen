import axios from "axios";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8001";

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 180000,
});

export async function analyzeProjectFiles({ files, model, outputFormat = "md" }) {
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));
  formData.append("model", model);
  formData.append("output_format", outputFormat);

  try {
    const response = await api.post("/api/v1/analyze", formData, {
      headers: { "Content-Type": "multipart/form-data" },
    });
    return response.data;
  } catch (error) {
    if (error.response?.data?.detail) {
      throw new Error(error.response.data.detail);
    }
    throw new Error("Failed to fetch: backend недоступен или блокируется CORS");
  }
}
