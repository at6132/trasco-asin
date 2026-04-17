import HomeClient from "./home-client";

/** Server shell: client UI lives in `home-client.tsx` so Next does not pass `params` / `searchParams` Promises onto the client root (stops devtools / inspector enumeration warnings). */
export default function Page() {
  return <HomeClient />;
}
