#!/usr/bin/env python3
"""Test script for action-card processing methods."""

from factories import DataFactory


def test_action_card_processing():
    """Test the action-card processing methods with sample data."""

    # Sample action-card data (similar to what was provided)
    sample_card = {
        "id": "card-123",
        "title": "Sample Action Card",
        "content": {
            "blocks": [
                {
                    "key": "abc123",
                    "text": "This is a sample action card with formatting.",
                    "type": "unstyled",
                    "depth": 0,
                    "inlineStyleRanges": [
                        {"offset": 10, "length": 6, "style": "BOLD"},
                        {"offset": 35, "length": 10, "style": "ITALIC"},
                    ],
                    "entityRanges": [],
                    "data": {},
                }
            ],
            "entityMap": {},
        },
        "language": "en",
        "adapted": False,
        "translated": False,
    }

    # Sample action-card document structure
    sample_doc = {
        "description": "Sample Action Card Document",
        "icon": "",
        "langId": "en",
        "LastUpdatedBy": "TestUser",
        "chapters": [{"description": "Chapter 1", "cards": [sample_card]}],
    }

    # Initialize factory
    factory = DataFactory()

    # Test the conversion method
    try:
        markdown = factory._convert_card_to_markdown(sample_card, "content")
        print("Markdown conversion successful:")
        print(markdown)
        print()

        # Test resource creation
        resources = factory.create_action_card_resources(sample_doc)
        print(f"Created {len(resources)} resources:")
        for resource in resources:
            print(f"- {resource.title} ({resource.language_id})")

    except Exception as e:
        print(f"Error during testing: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    test_action_card_processing()
