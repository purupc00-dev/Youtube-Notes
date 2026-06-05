const { Harbor } = require('@zonko-ai/harbor');

async function buildRepo() {
    const hrbr = new Harbor();
    console.log("🛡️ Initializing Secure Cloud Link...");
    
    // Instructing Harbor to use the GitHub tool via natural language
    const result = await hrbr.chat("Create a new private GitHub repository named 'Dada'.");
    
    console.log("\n✅ Execution Complete:");
    console.log(result.text);
}

buildRepo();