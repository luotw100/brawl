// 入口：仅 #/stage（观众画面，录制/直播都渲染这一页）出画面；
// 裸根、空 hash、未知路由一律纯黑空白。
import React from "react";
import ReactDOM from "react-dom/client";
import Stage from "./stage/Stage";

function App() {
  const [route, setRoute] = React.useState(location.hash);
  React.useEffect(() => {
    const onHash = () => setRoute(location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  if (route.startsWith("#/stage")) return <Stage />;
  return null; // 非显式路由：不画任何壳，纯黑空白
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
