---
name: image_generation
description: Generate images from text descriptions and send them via WhatsApp
---

# Image Generation

You have a `generate_image_tool` that creates images from text descriptions.

## When to Use
- User says "genera una imagen de...", "dibuja...", "crea una imagen..."
- User asks for visual content: logos, illustrations, photos, art
- User wants to visualize something

## When NOT to Use
- User asks to read/analyze an existing image (use vision instead)
- User wants a screenshot or real photo
- The request is inappropriate or violates content policies

## Rules
- Write prompts in ENGLISH for best results (translate user's request)
- Be specific: "a golden retriever puppy playing in autumn leaves, warm lighting, photographic style"
- Keep prompts under 200 words
- The image is sent directly to the user via WhatsApp
- Generation takes 5-15 seconds — tell the user you're generating
