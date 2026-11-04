/** Build-time public profile; Next embeds the same value for SSR and the browser. */
export function isVisitorEdition(): boolean {
  return process.env.NEXT_PUBLIC_DATA_PROFILE === "visitor";
}

export const RUN_LOCALLY_URL =
  "https://github.com/Rachit-2216/DOWNFORCE#run-locally";
