#!/usr/bin/env python3
"""Test script to demonstrate action-card slug generation."""

from factories import DataFactory


def test_slug_generation():
    """Test that action-card slugs are generated correctly."""

    # Sample action-card document
    sample_doc = {
        "description": "Test Action Card",
        "langId": "en",
        "chapters": [
            {
                "description": "Chapter 1",
                "cards": [
                    {
                        "id": "card-1",
                        "content": {"blocks": [{"text": "Test content"}]},
                        "adapted": {"blocks": [{"text": "Adapted content"}]},
                        "translated": {"blocks": [{"text": "Translated content"}]},
                    }
                ],
            }
        ],
    }

    # Create resources
    resources = DataFactory.create_action_card_resources(sample_doc)

    print("Generated Action-Card Resources:")
    print("=" * 50)

    for i, resource in enumerate(resources, 1):
        # Simulate slug generation (same logic as in ResourceMigrator)
        title_slug = (
            resource.title.lower()
            .replace(" ", "-")
            .replace("(", "")
            .replace(")", "")
            .replace(",", "")
        )
        action_card_key = "test-key-123"  # Would come from module doc
        slug = f"res-action-card-{title_slug}-{action_card_key}"

        print(f"Resource {i}:")
        print(f"  Title: {resource.title}")
        print(f"  Generated Slug: {slug}")
        print(f"  Content Type: {resource.content_type}")
        print(f"  Language: {resource.language_id}")
        print()

    print("Slug Tracking Logic:")
    print("- Slugs include: res-action-card-{title}-{action_card_key}")
    print("- Title is cleaned (lowercase, spaces to hyphens, remove brackets)")
    print("- Action card key ensures uniqueness across different cards")
    print("- If slug exists in mapping, uses UPDATE_RESOURCE (PATCH)")
    print("- If slug doesn't exist, uses POST_RESOURCE and saves mapping")


if __name__ == "__main__":
    test_slug_generation()
