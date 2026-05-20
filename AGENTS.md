# Project Instructions

## Coding Style

- Prefer object-oriented code. Model domain behavior with classes and objects instead of expanding procedural code.
- Keep functions and methods generic and reusable. Favor parameterized behavior over object-specific duplication.
- Structure implementations with the pattern `Domain -> Object -> Function`.
- Domain modules define the business context.
- Objects define the entities, services, or handlers inside that domain.
- Functions and methods implement behavior for those objects.
- When a method or function needs to work with a specific object, pass `object_name` explicitly as an argument instead of hardcoding the object selection.
- Use descriptive names that reflect the domain and the object being handled.

## Python Conventions

- Prefer classes for domain services, handlers, repositories, coordinators, and processors.
- Avoid procedural top-level orchestration when the logic belongs to a domain object or service object.
- Use generic method and function names such as `load_object`, `process_object`, `store_object`, or `dispatch_object` when the behavior applies to multiple objects.
- Pass `object_name` explicitly to methods and functions whenever behavior depends on the handled object.
- Do not create separate functions for each object when one generic function with `object_name` can express the behavior.
- Keep domain logic inside domain objects or domain services rather than unrelated utility modules.
- Separate object selection from object behavior: resolve the target object first, then execute reusable behavior through methods or functions.
- Prefer extending shared classes or composing reusable service objects over copying similar object-specific logic.
- Preserve existing public function APIs as compatibility wrappers when refactoring internals into objects or services.

## Anti-Patterns

- Avoid hardcoded object-specific branching spread across unrelated modules.
- Avoid object-specific function names such as `handle_bmw()` when a generic function like `handle_object(object_name, payload)` is sufficient.
- Avoid mixing domain definition, object instantiation, and function behavior into one large procedural function.

## Example Pattern

```python
class PromptService:
	def load_object(self, object_name: str) -> str:
		return get_system_prompt(prompt_name)


def dispatch_object(object_name: str, service: PromptService) -> str:
	return service.load_object(object_name)
```

- Domain: prompt handling
- Object: `PromptService`
- Function: `load_object(object_name)` and `dispatch_object(object_name, service)`
