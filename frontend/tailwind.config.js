/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx,ts,tsx}"],
  theme: {
    extend: {
      boxShadow: {
        glass: "0 8px 32px rgba(0, 0, 0, 0.35)",
      },
      colors: {
        panel: "rgba(15, 23, 42, 0.55)",
      },
    },
  },
  plugins: [],
};
