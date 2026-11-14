import React from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/overpass";
import "@fontsource-variable/source-serif-4";
import "./styles.css";
import App from "./App.jsx";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
