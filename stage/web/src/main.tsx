// 入口：仅 #/stage（观众画面，OBS 捕获这个）/ #/console（导播台）出画面；
// 裸根、空 hash、未知路由一律纯黑空白——门控的纵深兜底，配合服务端 GET / 的 token 硬门。
import React from "react";
import ReactDOM from "react-dom/client";
import Console from "./console/Console";
import Stage from "./stage/Stage";

function App() {
  const [route, setRoute] = React.useState(location.hash);
  React.useEffect(() => {
    const onHash = () => setRoute(location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  if (route.startsWith("#/console")) return <Console />;
  if (route.startsWith("#/stage")) return <Stage />;
  return null; // 非显式路由：不画任何壳，纯黑空白
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
